"""The MusicBrainz web service.

No key, but a meaningful `User-Agent` is mandatory and the rate limit of
one request a second is enforced by them, not merely suggested. Strong on
credits and on release detail; ISRCs exist but are not in the search
response, so getting one costs a second request per candidate.
"""

from typing import Any

from music_match.config import env
from music_match.sources import base
from music_match.sources import http
from music_match.tagging.fields import TrackTags

SEARCH_URL = "https://musicbrainz.org/ws/2/recording"
LOOKUP_URL = "https://musicbrainz.org/ws/2/recording/{recording_id}"

USER_AGENT_VAR = "MUSICBRAINZ_USER_AGENT"

# MusicBrainz allows one request per second averaged over time. Anything
# faster gets throttled, so this is a hard floor rather than a guess.
MIN_INTERVAL_SECONDS = 1.1

_FALLBACK_USER_AGENT = "music-match/0.1.0 ( https://github.com/stixvish )"


class MusicBrainzSource(base.MetadataSource):
  """Looks recordings up in MusicBrainz."""

  name = "musicbrainz"

  def __init__(self,
               client: http.HttpClient | None = None,
               *,
               fetch_isrcs: bool = True) -> None:
    """Sets up the source.

    Args:
      client: HTTP client to use. One is created if not given.
      fetch_isrcs: Whether to spend an extra request per candidate
        looking up its ISRCs, which the search response omits.
    """
    super().__init__(client or http.HttpClient(
        user_agent=env.get(USER_AGENT_VAR) or _FALLBACK_USER_AGENT,
        min_interval_seconds=MIN_INTERVAL_SECONDS))
    self._fetch_isrcs = fetch_isrcs

  def is_available(self) -> bool:
    """Returns True: MusicBrainz needs no key, only a user agent."""
    return True

  def search(self,
             query: base.SourceQuery,
             limit: int = 3) -> list[base.SourceResult]:
    """Searches MusicBrainz for a recording.

    Args:
      query: What is known about the track.
      limit: The most candidates to return.

    Returns:
      Candidates, best first by MusicBrainz's own score.

    Raises:
      SourceError: If the service could not be reached or answered
        unusably.
    """
    if not query.is_usable():
      return []
    try:
      body = self._client.get_json(SEARCH_URL,
                                   params={
                                       "query": lucene_query(query),
                                       "fmt": "json",
                                       "limit": limit,
                                   })
    except http.HttpError as err:
      raise base.SourceError(f"musicbrainz: {err}") from err

    recordings = body.get("recordings") if isinstance(body, dict) else None
    if not isinstance(recordings, list):
      return []
    return [
        self._to_result(item)
        for item in recordings[:limit]
        if isinstance(item, dict)
    ]

  def _to_result(self, item: dict[str, Any]) -> base.SourceResult:
    """Converts one MusicBrainz recording into a SourceResult.

    Args:
      item: One entry from the search response's `recordings`.

    Returns:
      The converted candidate.
    """
    recording_id = str(item.get("id", ""))
    release = _first_release(item.get("releases"))
    date = release.get("date") or item.get("first-release-date")
    media = _first_medium(release.get("media"))
    tags = TrackTags(
        title=item.get("title"),
        artist=_credit_name(item.get("artist-credit")),
        album=release.get("title"),
        album_artist=_credit_name(release.get("artist-credit")),
        isrc=self._isrc_for(recording_id),
        release_date=date if _is_full_date(date) else None,
        year=int(str(date)[:4]) if date else None,
        track_number=media.get("position"),
        track_total=release.get("track-count"),
    )
    length = item.get("length")
    return base.SourceResult(
        source=self.name,
        source_id=recording_id,
        tags=tags,
        duration_seconds=float(length) / 1000 if length else None,
        extra={"release_id": str(release.get("id", ""))} if release else {})

  def _isrc_for(self, recording_id: str) -> str | None:
    """Looks up a recording's ISRC, which the search response omits.

    Costs one extra rate-limited request, so it is skippable.

    Args:
      recording_id: The MusicBrainz recording id.

    Returns:
      The first ISRC, or None if there is none or the lookup failed.
    """
    if not self._fetch_isrcs or not recording_id:
      return None
    try:
      body = self._client.get_json(LOOKUP_URL.format(recording_id=recording_id),
                                   params={
                                       "inc": "isrcs",
                                       "fmt": "json"
                                   })
    except http.HttpError:
      # An ISRC is a bonus here, not the point of the query; losing one
      # must not lose the candidate.
      return None
    isrcs = body.get("isrcs") if isinstance(body, dict) else None
    if isinstance(isrcs, list) and isrcs:
      return str(isrcs[0])
    return None


def lucene_query(query: base.SourceQuery) -> str:
  """Builds a MusicBrainz search query.

  The service takes Lucene syntax, which is far more precise than free
  text when the artist is known.

  Args:
    query: What is known about the track.

  Returns:
    The query string.
  """
  parts = []
  if query.title:
    parts.append(f'recording:"{_escape(query.title)}"')
  if query.artist:
    parts.append(f'artist:"{_escape(query.artist)}"')
  return " AND ".join(parts) if parts else _escape(query.as_text())


def _escape(text: str) -> str:
  """Escapes Lucene special characters in a search term.

  Args:
    text: The raw term.

  Returns:
    The term with quotes and backslashes escaped.
  """
  return text.replace("\\", "\\\\").replace('"', '\\"')


def _credit_name(credit: Any) -> str | None:
  """Joins a MusicBrainz artist credit into a display name.

  The credit array preserves the join phrases — "Artist A feat. Artist B"
  — which is exactly what belongs in an artist tag.

  Args:
    credit: The `artist-credit` array.

  Returns:
    The joined name, or None if there is none.
  """
  if not isinstance(credit, list) or not credit:
    return None
  parts = []
  for entry in credit:
    if not isinstance(entry, dict):
      continue
    name = entry.get("name") or (entry.get("artist") or {}).get("name")
    if name:
      parts.append(str(name))
      parts.append(str(entry.get("joinphrase") or ""))
  joined = "".join(parts).strip()
  return joined or None


def _first_release(releases: Any) -> dict[str, Any]:
  """Returns the first release a recording appeared on.

  Args:
    releases: The `releases` array.

  Returns:
    The first release, or an empty mapping if there are none.
  """
  if isinstance(releases, list):
    for release in releases:
      if isinstance(release, dict):
        return release
  return {}


def _first_medium(media: Any) -> dict[str, Any]:
  """Returns the first medium of a release.

  Args:
    media: The `media` array.

  Returns:
    The first medium's first track info, or an empty mapping.
  """
  if isinstance(media, list):
    for medium in media:
      if isinstance(medium, dict):
        tracks = medium.get("track")
        if isinstance(tracks, list) and tracks and isinstance(tracks[0], dict):
          return tracks[0]
  return {}


def _is_full_date(value: Any) -> bool:
  """Returns whether a date carries more than a bare year.

  Args:
    value: A MusicBrainz date string, or None.

  Returns:
    True if it is longer than four characters.
  """
  return isinstance(value, str) and len(value) > 4
