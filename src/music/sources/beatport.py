"""Beatport adapter.

Ranked first for genre on electronic material (SPEC.md §7): Discogs covers only
~14% of recent digital-only electronic releases, which is most of what gets
added going forward.

**Keyed on ISRC, not text search.** `/v4/catalog/tracks/store/{isrc}` resolves a
track exactly, and resolution already recovers ISRC for ~97% of tracks — so this
is the highest-precision lookup available from any source. Search is the
fallback.

Two things about Beatport's auth are unusual and both are load-bearing:

1. **Tokens live 600 seconds.** A full-library run takes hours, so refresh is
   proactive on a timer, never reactive on a 401.
2. **Refresh tokens are single-use.** Each refresh revokes the old one and
   returns a new one, which must be persisted immediately or access is lost
   until a fresh authorization.

Auth is on a different host from the catalog: tokens come from
`account.beatport.com`, API calls go to `api.beatport.com`.

The response parsers below are written against Beatport's published schema and
have **not** been verified against a live response — no credentials have been
issued yet. Verify `parse_track` against real output before trusting it.
"""

import json
import logging
import sqlite3
import time
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass

from music.sources import cache
from music.sources.base import FieldCandidate, Identity
from music.sources.ratelimit import RateLimiter, with_backoff

log = logging.getLogger(__name__)

TOKEN_URL = "https://account.beatport.com/o/token/"
API = "https://api.beatport.com/v4"
NAME = "beatport"

# tokens are documented as expiring in 600s; refresh with margin to spare.
REFRESH_MARGIN_S = 90


class BeatportCredentialsError(RuntimeError):
  """Raised when no usable credentials are configured."""


@dataclass
class Token:
  """An access token and when it stops being usable."""

  access_token: str
  expires_at: float

  @property
  def is_fresh(self) -> bool:
    """Whether the token can still be used, with refresh margin applied."""
    return bool(self.access_token) and time.time() < self.expires_at - REFRESH_MARGIN_S


def parse_track(track: dict) -> list[FieldCandidate]:
  """Build field candidates from a Beatport track object.

  Prefers `sub_genre` over `genre`: the top-level genre is coarse
  ("Dance / Pop"), which is what purchased-file tags carry, while the sub-genre
  is the taxonomy worth having (SPEC.md §7).

  Args:
    track: A track object.

  Returns:
    Candidates, possibly empty.
  """
  out: list[FieldCandidate] = []

  def add(field: str, value: object) -> None:
    if value not in (None, "", []):
      out.append(FieldCandidate(field=field, value=str(value), source=NAME))

  add("title", track.get("name"))
  mix = track.get("mix_name")
  add("mix_name", mix)

  artists = [a.get("name") for a in (track.get("artists") or []) if a.get("name")]
  if artists:
    add("artist", artists[0])
  remixers = [r.get("name") for r in (track.get("remixers") or []) if r.get("name")]
  if remixers:
    add("remixer", remixers[0])

  sub_genre = (track.get("sub_genre") or {}).get("name")
  genre = (track.get("genre") or {}).get("name")
  add("genre", sub_genre or genre)

  release = track.get("release") or {}
  add("album", release.get("name"))
  label = (release.get("label") or {}).get("name") or (track.get("label") or {}).get(
    "name"
  )
  add("label", label)
  add("catalog_number", release.get("catalog_number"))

  published = track.get("publish_date") or track.get("new_release_date") or ""
  if published:
    add("release_date", str(published)[:10])
    add("year", str(published)[:4])

  add("isrc", track.get("isrc"))
  add("bpm", track.get("bpm"))
  key = track.get("key") or {}
  add("key", key.get("camelot_number") and _camelot(key) or key.get("name"))
  return out


def _camelot(key: dict) -> str:
  """Render a Beatport key object in Camelot notation, if it has the parts."""
  number = key.get("camelot_number")
  letter = key.get("camelot_letter")
  return f"{number}{letter}" if number and letter else ""


class Beatport:
  """Lookup against the Beatport v4 catalog API."""

  name = NAME

  def __init__(
    self, conn: sqlite3.Connection, client_id: str, client_secret: str
  ) -> None:
    """Initialise the adapter.

    Args:
      conn: Open connection, used for the response cache.
      client_id: OAuth client id issued by Beatport.
      client_secret: OAuth client secret issued by Beatport.

    Raises:
      BeatportCredentialsError: If either credential is missing.
    """
    if not client_id or not client_secret:
      raise BeatportCredentialsError(
        "beatport needs BEATPORT_CLIENT_ID and BEATPORT_CLIENT_SECRET; "
        "request them via the partner portal"
      )
    self._conn = conn
    self._id = client_id
    self._secret = client_secret
    self._token = Token("", 0.0)
    self._limiter = RateLimiter(per_second=2.0)

  def _access_token(self) -> str:
    """Return a usable access token, fetching a new one when needed."""
    if self._token.is_fresh:
      return self._token.access_token
    body = urllib.parse.urlencode(
      {
        "grant_type": "client_credentials",
        "client_id": self._id,
        "client_secret": self._secret,
      }
    ).encode()
    request = urllib.request.Request(
      TOKEN_URL,
      data=body,
      headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
      payload = json.load(response)
    self._token = Token(
      access_token=str(payload["access_token"]),
      expires_at=time.time() + float(payload.get("expires_in", 600)),
    )
    return self._token.access_token

  def _get(self, path: str, params: dict[str, str] | None = None) -> dict:
    def fetch() -> dict:
      self._limiter.wait()
      query = f"?{urllib.parse.urlencode(params)}" if params else ""
      request = urllib.request.Request(
        f"{API}{path}{query}",
        headers={"Authorization": f"Bearer {self._access_token()}"},
      )
      with urllib.request.urlopen(request, timeout=30) as response:
        return dict(json.load(response))

    return dict(
      cache.cached(
        self._conn,
        NAME,
        (path, tuple(sorted((params or {}).items()))),
        lambda: with_backoff(fetch),
      )
    )

  def by_isrc(self, isrc: str) -> dict | None:
    """Resolve a track by ISRC — an exact join, not a fuzzy match.

    Args:
      isrc: The recording's ISRC.

    Returns:
      The track object, or None if Beatport does not carry it.
    """
    try:
      payload = self._get(f"/catalog/tracks/store/{isrc}/")
    except Exception as exc:  # noqa: BLE001 - a miss is not an error
      log.debug("beatport isrc lookup failed for %s: %s", isrc, exc)
      return None
    results = payload.get("results")
    if isinstance(results, list):
      return results[0] if results else None
    return payload or None

  def search(self, identity: Identity) -> dict | None:
    """Fall back to text search when no ISRC is available.

    Args:
      identity: Normalised artist and title.

    Returns:
      The best track object, or None.
    """
    try:
      payload = self._get(
        "/catalog/search/",
        {"q": f"{identity.artist} {identity.title}".strip(), "type": "tracks"},
      )
    except Exception as exc:  # noqa: BLE001 - a dead source must not stop a run
      log.warning("beatport search failed: %s", exc)
      return None
    tracks = payload.get("tracks") or []
    return tracks[0] if tracks else None

  def lookup(self, identity: Identity) -> Sequence[FieldCandidate]:
    """Return candidates for a track, ISRC first.

    Args:
      identity: What we know so far.

    Returns:
      Candidates, empty on a miss.
    """
    track = self.by_isrc(identity.isrc) if identity.isrc else None
    if track is None:
      track = self.search(identity)
    return parse_track(track) if track else []
