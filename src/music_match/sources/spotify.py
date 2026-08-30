"""The Spotify Web API.

Needs a client ID and secret, exchanged for a bearer token that lasts an
hour. Strong on mainstream and recent releases, and the only source here
that returns an **ISRC straight from the search response** rather than
needing a second lookup. Carries no credits — no remixer, no composer.
"""

import base64
import time
from typing import Any

from music_match.config import env
from music_match.sources import base
from music_match.sources import http
from music_match.tagging.fields import TrackTags

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"

CLIENT_ID_VAR = "SPOTIFY_CLIENT_ID"
CLIENT_SECRET_VAR = "SPOTIFY_CLIENT_SECRET"

MIN_INTERVAL_SECONDS = 0.2

# Refresh a little before the hour is up rather than racing the expiry.
_TOKEN_MARGIN_SECONDS = 60.0


class SpotifySource(base.MetadataSource):
  """Looks tracks up in the Spotify Web API."""

  name = "spotify"

  def __init__(self, client: http.HttpClient | None = None) -> None:
    """Sets up the source.

    Args:
      client: HTTP client to use. One is created if not given.
    """
    super().__init__(client or
                     http.HttpClient(user_agent="music-match/0.1.0",
                                     min_interval_seconds=MIN_INTERVAL_SECONDS))
    self._token: str | None = None
    self._token_expires_at = 0.0

  def is_available(self) -> bool:
    """Returns whether both Spotify credentials are configured."""
    return env.has(CLIENT_ID_VAR, CLIENT_SECRET_VAR)

  def search(self,
             query: base.SourceQuery,
             limit: int = 3) -> list[base.SourceResult]:
    """Searches Spotify for a track.

    Args:
      query: What is known about the track.
      limit: The most candidates to return.

    Returns:
      Candidates, best first.

    Raises:
      SourceError: If credentials are missing, or the API could not be
        reached.
    """
    if not query.is_usable():
      return []
    try:
      token = self._access_token()
      body = self._client.get_json(SEARCH_URL,
                                   params={
                                       "q": search_expression(query),
                                       "type": "track",
                                       "limit": limit,
                                   },
                                   headers={"Authorization": f"Bearer {token}"})
    except (http.HttpError, env.MissingCredential) as err:
      raise base.SourceError(f"spotify: {err}") from err

    tracks = body.get("tracks") if isinstance(body, dict) else None
    items = tracks.get("items") if isinstance(tracks, dict) else None
    if not isinstance(items, list):
      return []
    return [
        self._to_result(item)
        for item in items[:limit]
        if isinstance(item, dict)
    ]

  def _access_token(self) -> str:
    """Returns a valid bearer token, fetching one if needed.

    Returns:
      The access token.

    Raises:
      MissingCredential: If the credentials are not configured.
      HttpError: If the token exchange fails.
    """
    if self._token and time.monotonic() < self._token_expires_at:
      return self._token
    client_id = env.require(CLIENT_ID_VAR, "spotify")
    secret = env.require(CLIENT_SECRET_VAR, "spotify")
    credentials = base64.b64encode(
        f"{client_id}:{secret}".encode("utf-8")).decode("ascii")
    body = self._client.post_json(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {credentials}"})
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str):
      raise http.HttpError("spotify returned no access token")
    lifetime = float(body.get("expires_in", 3600))
    self._token = token
    self._token_expires_at = (time.monotonic() + lifetime -
                              _TOKEN_MARGIN_SECONDS)
    return token

  def _to_result(self, item: dict[str, Any]) -> base.SourceResult:
    """Converts one Spotify track into a SourceResult.

    Args:
      item: One entry from the search response's `tracks.items`.

    Returns:
      The converted candidate.
    """
    album = item.get("album") or {}
    artists = _names(item.get("artists"))
    album_artists = _names(album.get("artists"))
    release_date = album.get("release_date")
    art_url, art_size = _largest_image(album.get("images"))
    tags = TrackTags(
        title=item.get("name"),
        artist=", ".join(artists) or None,
        album=album.get("name"),
        album_artist=", ".join(album_artists) or None,
        isrc=(item.get("external_ids") or {}).get("isrc"),
        release_date=release_date if _is_full_date(release_date) else None,
        year=int(str(release_date)[:4]) if release_date else None,
        track_number=item.get("track_number"),
        track_total=album.get("total_tracks"),
        disc_number=item.get("disc_number"),
    )
    millis = item.get("duration_ms")
    return base.SourceResult(source=self.name,
                             source_id=str(item.get("id", "")),
                             tags=tags,
                             duration_seconds=float(millis) /
                             1000 if millis else None,
                             art_url=art_url,
                             art_size=art_size)


def _names(artists: Any) -> list[str]:
  """Extracts artist names from a Spotify artist array.

  Args:
    artists: The `artists` array from a track or album.

  Returns:
    The names, in order, skipping malformed entries.
  """
  if not isinstance(artists, list):
    return []
  return [
      str(artist["name"])
      for artist in artists
      if isinstance(artist, dict) and artist.get("name")
  ]


def search_expression(query: base.SourceQuery) -> str:
  """Builds a Spotify search expression.

  Spotify supports field filters, which are far more precise than free
  text when the artist is known.

  Args:
    query: What is known about the track.

  Returns:
    The search string.
  """
  parts = []
  if query.title:
    parts.append(f'track:"{query.title}"')
  if query.artist:
    parts.append(f'artist:"{query.artist}"')
  return " ".join(parts) if parts else query.as_text()


def _is_full_date(value: Any) -> bool:
  """Returns whether a Spotify release date carries more than a year.

  Args:
    value: The `release_date` field, which may be "1997", "1997-01" or a
      full date depending on the album's precision.

  Returns:
    True if it is longer than a bare year.
  """
  return isinstance(value, str) and len(value) > 4


def _largest_image(images: Any) -> tuple[str | None, int | None]:
  """Picks the biggest cover image Spotify offers.

  Args:
    images: The album's `images` array.

  Returns:
    (url, width), or (None, None) if there are none.
  """
  if not isinstance(images, list):
    return (None, None)
  sized = [(image.get("width") or 0, image.get("url"))
           for image in images
           if isinstance(image, dict) and image.get("url")]
  if not sized:
    return (None, None)
  width, url = max(sized)
  return (url, width or None)
