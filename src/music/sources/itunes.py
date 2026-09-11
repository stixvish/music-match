"""iTunes Search adapter.

Free, no signup, no auth — a plain HTTP GET. Distinct from the Apple Music API,
which needs a paid developer account and is out of scope (SPEC.md §3).

§7 ranks it first for artwork, and it has the strongest coverage of the
library's Bollywood material, where MusicBrainz and Discogs are both thin.
"""

import json
import logging
import sqlite3
import urllib.parse
import urllib.request
from collections.abc import Sequence

from music.sources import cache
from music.sources.base import FieldCandidate, Identity
from music.sources.ratelimit import RateLimiter, with_backoff

log = logging.getLogger(__name__)

API = "https://itunes.apple.com/search"
NAME = "itunes"

# the api returns a 100px thumbnail; the same url serves any size.
ARTWORK_SIZE = "1200x1200"


def upscale_artwork(url: str, size: str = ARTWORK_SIZE) -> str:
  """Rewrite an artwork URL to request a larger image.

  Args:
    url: An `artworkUrl100`-style URL.
    size: Replacement dimension string.

  Returns:
    The rewritten URL, or the original if it has no recognisable size.
  """
  for known in ("100x100", "60x60", "30x30"):
    if known in url:
      return url.replace(known, size)
  return url


def candidates_from(result: dict) -> list[FieldCandidate]:
  """Build field candidates from one iTunes result.

  Args:
    result: A single search result.

  Returns:
    Candidates, possibly empty.
  """
  out: list[FieldCandidate] = []
  mapping = {
    "title": "trackName",
    "artist": "artistName",
    "album": "collectionName",
    "album_artist": "collectionArtistName",
    "genre": "primaryGenreName",
  }
  for field_name, key in mapping.items():
    value = result.get(key)
    if value:
      out.append(FieldCandidate(field=field_name, value=str(value), source=NAME))

  if result.get("trackNumber"):
    out.append(
      FieldCandidate(
        field="track_number", value=str(result["trackNumber"]), source=NAME
      )
    )
  if result.get("discNumber"):
    out.append(
      FieldCandidate(field="disc_number", value=str(result["discNumber"]), source=NAME)
    )
  released = str(result.get("releaseDate") or "")
  if released:
    out.append(FieldCandidate(field="release_date", value=released[:10], source=NAME))
    out.append(FieldCandidate(field="year", value=released[:4], source=NAME))
  art = result.get("artworkUrl100")
  if art:
    out.append(
      FieldCandidate(field="artwork_url", value=upscale_artwork(str(art)), source=NAME)
    )
  return out


class ITunes:
  """Lookup against the free iTunes Search API."""

  name = NAME

  def __init__(self, conn: sqlite3.Connection) -> None:
    """Initialise the adapter.

    Args:
      conn: Open connection, used for the response cache.
    """
    self._conn = conn
    # apple throttles around 20 requests per minute
    self._limiter = RateLimiter(per_second=0.33)

  def _get(self, params: dict[str, str]) -> dict:
    def fetch() -> dict:
      self._limiter.wait()
      url = f"{API}?{urllib.parse.urlencode(params)}"
      request = urllib.request.Request(url, headers={"User-Agent": "music-match/0.1"})
      with urllib.request.urlopen(request, timeout=30) as response:
        return dict(json.loads(response.read().decode("utf-8", "replace")))

    return dict(
      cache.cached(
        self._conn, NAME, tuple(sorted(params.items())), lambda: with_backoff(fetch)
      )
    )

  def lookup(self, identity: Identity) -> Sequence[FieldCandidate]:
    """Return candidates for a track.

    Args:
      identity: Normalised artist and title.

    Returns:
      Candidates, empty on a miss.
    """
    params = {
      "term": f"{identity.artist} {identity.title}".strip(),
      "entity": "song",
      "limit": "5",
    }
    try:
      payload = self._get(params)
    except Exception as exc:  # noqa: BLE001 - a dead source must not stop a run
      log.warning("itunes lookup failed: %s", exc)
      return []
    results = payload.get("results") or []
    return candidates_from(results[0]) if results else []
