"""Resolve a track's identity, strongest evidence first (SPEC.md §12).

Order: manual url override, ISRC, acoustic fingerprint, normalised text search.
Each step is tried only when the one above it produces nothing usable.
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from music.identify import Evidence, Match, variant_mismatch
from music.sources.acoustid import AcoustId
from music.sources.base import FieldCandidate, Identity
from music.sources.musicbrainz import MusicBrainz, select_release

log = logging.getLogger(__name__)

# how many of acoustid's linked mbids to inspect. a single fingerprint can map
# to a dozen near-duplicate musicbrainz entries for the same song.
MBID_PROBE = 3


@dataclass
class Resolution:
  """The outcome of resolving one track."""

  match: Match
  candidates: list[FieldCandidate] = field(default_factory=list)
  acoustid: str = ""
  mb_recording_id: str = ""


class Resolver:
  """Runs the evidence ladder over the available sources."""

  def __init__(
    self,
    conn: sqlite3.Connection,
    musicbrainz: MusicBrainz,
    acoustid: AcoustId | None = None,
  ) -> None:
    """Initialise the resolver.

    Args:
      conn: Open connection.
      musicbrainz: MusicBrainz adapter.
      acoustid: AcoustID adapter, if a key is configured.
    """
    self._conn = conn
    self._mb = musicbrainz
    self._acoustid = acoustid

  def resolve(self, identity: Identity, audio: Path | None = None) -> Resolution:
    """Identify a track.

    Args:
      identity: Normalised artist and title, ideally with duration.
      audio: The audio file, required for fingerprinting.

    Returns:
      The resolution, including the evidence behind it.
    """
    if audio is not None and self._acoustid is not None:
      resolved = self._by_fingerprint(identity, audio)
      if resolved is not None:
        return resolved
    match, candidates = self._mb.evaluate(identity)
    return Resolution(match=match, candidates=list(candidates))

  def _by_fingerprint(self, identity: Identity, audio: Path) -> Resolution | None:
    matches = self._acoustid.identify(audio) if self._acoustid else []
    if not matches:
      return None
    top = matches[0]

    best: tuple[dict, list] | None = None
    for mbid in top.recording_ids[:MBID_PROBE]:
      try:
        recording = self._mb.recording(mbid)
      except Exception as exc:  # noqa: BLE001 - try the next mbid
        log.debug("mbid lookup failed for %s: %s", mbid[:8], exc)
        continue
      releases = self._mb.releases_for(recording)
      if select_release(releases, identity.artist) is not None:
        best = (recording, releases)
        break
      if best is None:
        best = (recording, releases)
    if best is None:
      return None

    recording, _ = best
    candidates = list(self._mb.candidates_from(recording, identity))
    delta = None
    if identity.duration_s and recording.get("length"):
      delta = float(recording["length"]) / 1000 - identity.duration_s

    match = Match(
      evidence=Evidence.ACOUSTID,
      acoustid_score=top.score,
      # the fingerprint is computed over this exact file, so when the linked
      # recording has no length we treat the audio itself as the duration
      # evidence rather than penalising a strong acoustic match.
      duration_delta_s=0.0 if delta is None else delta,
      variant_mismatch=variant_mismatch(
        identity.title, str(recording.get("title") or "")
      ),
    )
    return Resolution(
      match=match,
      candidates=candidates,
      acoustid=top.acoustid,
      mb_recording_id=str(recording.get("id") or ""),
    )


def to_identity(
  artist: str, title: str, duration_s: float | None, artist_full: str = ""
) -> Identity:
  """Build an Identity from normalised parts.

  Args:
    artist: Primary artist.
    title: Cleaned title.
    duration_s: Duration of our audio.
    artist_full: Full credit, for retry.

  Returns:
    An Identity.
  """
  return Identity(
    artist=artist, title=title, duration_s=duration_s, artist_full=artist_full
  )


def candidate_map(candidates: Sequence[FieldCandidate]) -> dict[str, str]:
  """Flatten candidates into a field->value map, first value winning.

  Args:
    candidates: Candidates from a source.

  Returns:
    A plain mapping, for display and tests.
  """
  out: dict[str, str] = {}
  for candidate in candidates:
    out.setdefault(candidate.field, candidate.value)
  return out
