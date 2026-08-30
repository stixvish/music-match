"""The iTunes Search API.

No authentication and no key. Good for mainstream releases and for album
art, weak on anything a major label did not put out. Carries no ISRC and
no credits, so it is a fallback rather than a primary anywhere.
"""

from typing import Any

from music_match.sources import base
from music_match.sources import http
from music_match.tagging.fields import TrackTags

SEARCH_URL = "https://itunes.apple.com/search"

# Apple asks for no more than about twenty calls a minute.
MIN_INTERVAL_SECONDS = 3.0

# Artwork URLs come back at 100x100 but the size is just a path segment,
# and 640x640 is what Rekordbox wants embedded.
_ART_SIZE = 640


class ITunesSource(base.MetadataSource):
  """Looks tracks up in the iTunes Search API."""

  name = "itunes"

  def __init__(self, client: http.HttpClient | None = None) -> None:
    """Sets up the source.

    Args:
      client: HTTP client to use. One is created if not given.
    """
    super().__init__(client or
                     http.HttpClient(user_agent="music-match/0.1.0",
                                     min_interval_seconds=MIN_INTERVAL_SECONDS))

  def is_available(self) -> bool:
    """Returns True: this API needs no credentials."""
    return True

  def search(self,
             query: base.SourceQuery,
             limit: int = 3) -> list[base.SourceResult]:
    """Searches iTunes for a track.

    Args:
      query: What is known about the track.
      limit: The most candidates to return.

    Returns:
      Candidates, best first.

    Raises:
      SourceError: If the API could not be reached or answered unusably.
    """
    if not query.is_usable():
      return []
    try:
      body = self._client.get_json(SEARCH_URL,
                                   params={
                                       "term": query.as_text(),
                                       "entity": "song",
                                       "limit": limit,
                                   })
    except http.HttpError as err:
      raise base.SourceError(f"itunes: {err}") from err

    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
      return []
    return [
        self._to_result(item)
        for item in results[:limit]
        if isinstance(item, dict)
    ]

  def _to_result(self, item: dict[str, Any]) -> base.SourceResult:
    """Converts one iTunes result into a SourceResult.

    Args:
      item: One entry from the API's `results` array.

    Returns:
      The converted candidate.
    """
    release_date = str(item.get("releaseDate") or "")[:10] or None
    tags = TrackTags(
        title=item.get("trackName"),
        artist=item.get("artistName"),
        album=item.get("collectionName"),
        album_artist=item.get("collectionArtistName") or item.get("artistName"),
        genre=item.get("primaryGenreName"),
        release_date=release_date,
        year=int(release_date[:4]) if release_date else None,
        track_number=item.get("trackNumber"),
        track_total=item.get("trackCount"),
        disc_number=item.get("discNumber"),
        disc_total=item.get("discCount"),
    )
    millis = item.get("trackTimeMillis")
    return base.SourceResult(
        source=self.name,
        source_id=str(item.get("trackId", "")),
        tags=tags,
        duration_seconds=float(millis) / 1000 if millis else None,
        art_url=_upgrade_art(item.get("artworkUrl100")),
        art_size=_ART_SIZE if item.get("artworkUrl100") else None)


def _upgrade_art(url: str | None) -> str | None:
  """Rewrites an iTunes artwork URL to the size this tool embeds.

  Args:
    url: A `100x100bb` artwork URL, or None.

  Returns:
    The same URL at 640x640, or None.
  """
  if not url:
    return None
  return url.replace("100x100bb", f"{_ART_SIZE}x{_ART_SIZE}bb")
