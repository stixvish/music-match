"""Discogs adapter.

The best free source of electronic genre data. Discogs separates `genre`
(coarse: Electronic) from `style` (useful: Tech House, Melodic Techno), and
§7 ranks it first for style, label and catalogue number.

Free with a personal access token; 60 req/min authenticated on a rolling
window, and it requires a descriptive user-agent.
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

API = "https://api.discogs.com/database/search"
NAME = "discogs"


def parse_search(payload: dict) -> list[dict]:
  """Extract usable results from a search response.

  Args:
    payload: Decoded JSON.

  Returns:
    Result dicts.
  """
  return [r for r in (payload.get("results") or []) if isinstance(r, dict)]


def candidates_from(result: dict) -> list[FieldCandidate]:
  """Build field candidates from one Discogs result.

  `style` is preferred over `genre`: Discogs' genre is too coarse to be useful
  for a DJ ("Electronic"), while style is exactly the taxonomy we want.

  Args:
    result: A single search result.

  Returns:
    Candidates, possibly empty.
  """
  out: list[FieldCandidate] = []
  styles = [s for s in (result.get("style") or []) if s]
  genres = [g for g in (result.get("genre") or []) if g]
  if styles:
    out.append(FieldCandidate(field="genre", value=styles[0], source=NAME))
  elif genres:
    out.append(FieldCandidate(field="genre", value=genres[0], source=NAME))

  labels = [x for x in (result.get("label") or []) if x]
  if labels:
    out.append(FieldCandidate(field="label", value=labels[0], source=NAME))
  if result.get("catno"):
    out.append(
      FieldCandidate(field="catalog_number", value=str(result["catno"]), source=NAME)
    )
  if result.get("year"):
    out.append(FieldCandidate(field="year", value=str(result["year"]), source=NAME))
  return out


def select_result(results: Sequence[dict]) -> dict | None:
  """Pick which Discogs pressing to take label and year from.

  Search returns every pressing, including bootlegs and late reissues — a
  query for "Daft Punk - One More Time" returned label `Cybernetic`, catalogue
  `CYB18`, year 2010, rather than the original Virgin release. The earliest
  dated pressing is much more likely to be the original.

  Style is unaffected by this choice; every pressing carries the same one.

  Args:
    results: Search results.

  Returns:
    The chosen result, or None.
  """
  usable = [r for r in results if r]
  if not usable:
    return None
  dated = [r for r in usable if str(r.get("year") or "").isdigit()]
  if not dated:
    return usable[0]
  return min(dated, key=lambda r: int(r["year"]))


class Discogs:
  """Lookup against the Discogs database search."""

  name = NAME

  def __init__(
    self,
    conn: sqlite3.Connection,
    token: str,
    user_agent: str = "music-match/0.1 +https://github.com/stixvish/music-match",
  ) -> None:
    """Initialise the adapter.

    Args:
      conn: Open connection, used for the response cache.
      token: Discogs personal access token.
      user_agent: Sent on every request; Discogs rejects generic agents.
    """
    self._conn = conn
    self._token = token
    self._ua = user_agent
    self._limiter = RateLimiter(per_second=1.0)

  def _get(self, params: dict[str, str]) -> dict:
    def fetch() -> dict:
      self._limiter.wait()
      url = f"{API}?{urllib.parse.urlencode(params)}"
      request = urllib.request.Request(
        url,
        headers={
          "User-Agent": self._ua,
          "Authorization": f"Discogs token={self._token}",
        },
      )
      with urllib.request.urlopen(request, timeout=30) as response:
        return dict(json.load(response))

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
      Candidates, empty on a miss. Never raises for a simple miss.
    """
    params = {
      "artist": identity.artist,
      "track": identity.title,
      "type": "release",
      "per_page": "5",
    }
    try:
      payload = self._get(params)
    except Exception as exc:  # noqa: BLE001 - a dead source must not stop a run
      log.warning("discogs lookup failed: %s", exc)
      return []
    results = parse_search(payload)
    chosen = select_result(results)
    return candidates_from(chosen) if chosen else []
