"""AcoustID adapter: identify a track by its audio, not its filename.

Chromaprint fingerprints are fuzzy and are never compared locally. They go to
AcoustID, which returns a stable id plus linked MusicBrainz recording MBIDs —
turning fuzzy audio matching into an exact key lookup (SPEC.md §8).

This is the only source that works when the filename is useless, and it is the
one that covers Bollywood, where text search is weakest.
"""

import json
import logging
import sqlite3
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from music.sources import cache
from music.sources.ratelimit import RateLimiter, with_backoff

log = logging.getLogger(__name__)

API = "https://api.acoustid.org/v2/lookup"
NAME = "acoustid"

# the api accepts a SPACE-separated meta list. a "+" is urlencoded to %2B and
# silently ignored, returning status=ok with no metadata at all — which looks
# exactly like "fingerprint known but unlinked". measured the hard way.
META = "recordings releasegroups"


class FingerprintError(RuntimeError):
  """Raised when fpcalc cannot fingerprint a file."""


@dataclass(frozen=True)
class Fingerprint:
  """A Chromaprint fingerprint and the duration it was computed over."""

  fingerprint: str
  duration_s: int


@dataclass(frozen=True)
class AcoustIdMatch:
  """One AcoustID result."""

  acoustid: str
  score: float
  recording_ids: tuple[str, ...] = ()
  title: str = ""
  artist: str = ""

  @property
  def is_linked(self) -> bool:
    """Whether the fingerprint maps to any MusicBrainz recording.

    A high score with no linked recording means the audio is recognised but
    nobody has tied that upload to MusicBrainz. Music-video rips look like
    this — 0.997 and zero MBIDs — so an unlinked hit is not a usable identity.
    """
    return bool(self.recording_ids)


def fingerprint(path: Path) -> Fingerprint:
  """Compute a Chromaprint fingerprint with fpcalc.

  Args:
    path: Audio file.

  Returns:
    The fingerprint and its duration.

  Raises:
    FingerprintError: If fpcalc fails or returns nothing usable.
  """
  result = subprocess.run(
    ["fpcalc", "-json", str(path)], capture_output=True, text=True, check=False
  )
  if result.returncode != 0:
    raise FingerprintError(f"fpcalc failed on {path.name}: {result.stderr[:200]}")
  try:
    data = json.loads(result.stdout)
    return Fingerprint(str(data["fingerprint"]), int(float(data["duration"])))
  except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
    raise FingerprintError(f"unreadable fpcalc output for {path.name}") from exc


def parse_response(payload: dict) -> list[AcoustIdMatch]:
  """Turn an AcoustID response into matches, best score first.

  Near-duplicate MusicBrainz entries for the same song are collapsed by id, so
  five MBIDs for one recording do not later read as five rival candidates.

  Args:
    payload: Decoded JSON response.

  Returns:
    Matches, highest score first.
  """
  matches: list[AcoustIdMatch] = []
  for result in payload.get("results", []) or []:
    recordings = result.get("recordings") or []
    ids = tuple(dict.fromkeys(str(r["id"]) for r in recordings if r.get("id")))
    first = recordings[0] if recordings else {}
    artists = first.get("artists") or []
    matches.append(
      AcoustIdMatch(
        acoustid=str(result.get("id", "")),
        score=float(result.get("score") or 0.0),
        recording_ids=ids,
        title=str(first.get("title") or ""),
        artist=str(artists[0].get("name", "")) if artists else "",
      )
    )
  return sorted(matches, key=lambda m: -m.score)


class AcoustId:
  """Lookup against the AcoustID web service."""

  name = NAME

  def __init__(self, conn: sqlite3.Connection, api_key: str) -> None:
    """Initialise the adapter.

    Args:
      conn: Open connection, used for the response cache.
      api_key: AcoustID client key.
    """
    self._conn = conn
    self._key = api_key
    self._limiter = RateLimiter(per_second=3.0)

  def lookup_fingerprint(self, print_: Fingerprint) -> list[AcoustIdMatch]:
    """Identify audio by fingerprint.

    Args:
      print_: The fingerprint to look up.

    Returns:
      Matches, best first. Empty on a miss.
    """

    def fetch() -> dict:
      self._limiter.wait()
      body = urllib.parse.urlencode(
        {
          "client": self._key,
          "duration": str(print_.duration_s),
          "fingerprint": print_.fingerprint,
          "meta": META,
        }
      ).encode()
      request = urllib.request.Request(
        API, data=body, headers={"User-Agent": "music-match/0.1"}
      )
      with urllib.request.urlopen(request, timeout=30) as response:
        return dict(json.load(response))

    try:
      # cache on the fingerprint, not the file: two copies of the same audio
      # at different bitrates share one lookup.
      payload = cache.cached(
        self._conn,
        NAME,
        (print_.fingerprint[:64], print_.duration_s),
        lambda: with_backoff(fetch),
      )
    except Exception as exc:  # noqa: BLE001 - a dead source must not stop a run
      log.warning("acoustid lookup failed: %s", exc)
      return []
    if payload.get("status") != "ok":
      log.warning("acoustid returned %s", payload.get("status"))
      return []
    return parse_response(payload)

  def identify(self, path: Path) -> Sequence[AcoustIdMatch]:
    """Fingerprint a file and identify it.

    Args:
      path: Audio file.

    Returns:
      Matches with at least one linked MusicBrainz recording, best first.
    """
    try:
      print_ = fingerprint(path)
    except FingerprintError as exc:
      log.warning("%s", exc)
      return []
    return [m for m in self.lookup_fingerprint(print_) if m.is_linked]
