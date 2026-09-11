"""Spotify adapter.

Spotify's intelligence layer is gone — audio-features, audio-analysis,
recommendations and related-artists were closed to new apps in November 2024
and remain closed (SPEC.md §4). What survives is search and metadata lookup,
which is still excellent: the broadest catalogue of any free source, plus ISRC.

Its known weakness is that `artists[]` flattens collaborating and featured
artists into one list, so credits come from MusicBrainz instead (§7).
"""

import base64
import json
import logging
import sqlite3
import time
import urllib.parse
import urllib.request
from collections.abc import Sequence

from music.sources import cache
from music.sources.base import FieldCandidate, Identity, best_result
from music.sources.ratelimit import RateLimiter, with_backoff

log = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
NAME = "spotify"


def candidates_from(track: dict) -> list[FieldCandidate]:
  """Build field candidates from one Spotify track object.

  Args:
    track: A track object from the search response.

  Returns:
    Candidates, possibly empty.
  """
  out: list[FieldCandidate] = []
  if track.get("name"):
    out.append(FieldCandidate(field="title", value=str(track["name"]), source=NAME))

  artists = [a.get("name") for a in (track.get("artists") or []) if a.get("name")]
  if artists:
    # the primary artist only: spotify flattens featured and collaborating
    # artists into one list and does not say which is which (§7).
    out.append(FieldCandidate(field="artist", value=str(artists[0]), source=NAME))

  album = track.get("album") or {}
  if album.get("name"):
    out.append(FieldCandidate(field="album", value=str(album["name"]), source=NAME))
  album_artists = [a.get("name") for a in (album.get("artists") or []) if a.get("name")]
  if album_artists:
    out.append(
      FieldCandidate(field="album_artist", value=str(album_artists[0]), source=NAME)
    )
  released = str(album.get("release_date") or "")
  if released:
    out.append(FieldCandidate(field="release_date", value=released, source=NAME))
    out.append(FieldCandidate(field="year", value=released[:4], source=NAME))

  if track.get("track_number"):
    out.append(
      FieldCandidate(
        field="track_number", value=str(track["track_number"]), source=NAME
      )
    )
  if track.get("disc_number"):
    out.append(
      FieldCandidate(field="disc_number", value=str(track["disc_number"]), source=NAME)
    )
  isrc = (track.get("external_ids") or {}).get("isrc")
  if isrc:
    out.append(FieldCandidate(field="isrc", value=str(isrc), source=NAME))
  images = album.get("images") or []
  if images and images[0].get("url"):
    out.append(
      FieldCandidate(field="artwork_url", value=str(images[0]["url"]), source=NAME)
    )
  return out


class Spotify:
  """Lookup against the Spotify Web API using client credentials."""

  name = NAME

  def __init__(
    self, conn: sqlite3.Connection, client_id: str, client_secret: str
  ) -> None:
    """Initialise the adapter.

    Args:
      conn: Open connection, used for the response cache.
      client_id: Spotify application client id.
      client_secret: Spotify application client secret.
    """
    self._conn = conn
    self._id = client_id
    self._secret = client_secret
    self._token = ""
    self._expires_at = 0.0
    self._limiter = RateLimiter(per_second=5.0)

  def _access_token(self) -> str:
    """Fetch or reuse a client-credentials token."""
    if self._token and time.time() < self._expires_at - 30:
      return self._token
    auth = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
    request = urllib.request.Request(
      TOKEN_URL,
      data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
      headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
      },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
      payload = json.load(response)
    self._token = str(payload["access_token"])
    self._expires_at = time.time() + float(payload.get("expires_in", 3600))
    return self._token

  def _get(self, params: dict[str, str]) -> dict:
    def fetch() -> dict:
      self._limiter.wait()
      url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
      request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {self._access_token()}"}
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
      Candidates, empty on a miss.
    """
    query = f'track:"{identity.title}" artist:"{identity.artist}"'
    try:
      payload = self._get({"q": query, "type": "track", "limit": "5"})
    except Exception as exc:  # noqa: BLE001 - a dead source must not stop a run
      log.warning("spotify lookup failed: %s", exc)
      return []
    items = ((payload.get("tracks") or {}).get("items")) or []
    best = best_result(
      identity,
      items,
      artist=lambda r: str(((r.get("artists") or [{}])[0]).get("name") or ""),
      title=lambda r: str(r.get("name") or ""),
    )
    return candidates_from(best) if best else []
