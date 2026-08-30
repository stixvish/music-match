"""The Discogs database.

The primary source for electronic music, and the reason the local genre
model is the Discogs one: they share a taxonomy, so a detected
"Electronic---Deep House" and a Discogs style are the same vocabulary.

Discogs is also the only source here carrying **credits** — the
`extraartists` array on a release gives remixers, producers and writers by
role — and the only one with label and catalogue number. Those are exactly
the fields ARCHITECTURE expects it to win on.

Getting credits costs a second request: search returns releases, and the
tracklist and credits only come from a release lookup.
"""

from typing import Any

from music_match.config import env
from music_match.sources import base
from music_match.sources import http
from music_match.tagging.fields import TrackTags

SEARCH_URL = "https://api.discogs.com/database/search"
RELEASE_URL = "https://api.discogs.com/releases/{release_id}"

TOKEN_VAR = "DISCOGS_TOKEN"

# Discogs allows sixty authenticated requests a minute.
MIN_INTERVAL_SECONDS = 1.05

# Roles in a release's `extraartists` that map onto our target fields.
_REMIX_ROLES = ("remix", "remixed by")
_COMPOSER_ROLES = ("written-by", "composed by", "music by")
_LYRICIST_ROLES = ("lyrics by", "written-by", "words by")


class DiscogsSource(base.MetadataSource):
  """Looks releases up in the Discogs database."""

  name = "discogs"

  def __init__(self,
               client: http.HttpClient | None = None,
               *,
               fetch_credits: bool = True) -> None:
    """Sets up the source.

    Args:
      client: HTTP client to use. One is created if not given.
      fetch_credits: Whether to spend an extra request per candidate on
        the release lookup that carries credits and the tracklist.
    """
    self._client = client or http.HttpClient(
        user_agent="music-match/0.1.0 +https://github.com/stixvish",
        min_interval_seconds=MIN_INTERVAL_SECONDS)
    self._fetch_credits = fetch_credits

  def is_available(self) -> bool:
    """Returns whether a Discogs token is configured."""
    return env.has(TOKEN_VAR)

  def search(self,
             query: base.SourceQuery,
             limit: int = 3) -> list[base.SourceResult]:
    """Searches Discogs for a release.

    Args:
      query: What is known about the track.
      limit: The most candidates to return.

    Returns:
      Candidates, best first.

    Raises:
      SourceError: If the token is missing or the API could not be
        reached.
    """
    if not query.is_usable():
      return []
    try:
      token = env.require(TOKEN_VAR, "discogs")
      body = self._client.get_json(SEARCH_URL,
                                   params={
                                       "q": query.as_text(),
                                       "type": "release",
                                       "per_page": limit,
                                   },
                                   headers=_auth(token))
    except (http.HttpError, env.MissingCredential) as err:
      raise base.SourceError(f"discogs: {err}") from err

    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list):
      return []
    return [
        self._to_result(item, query, token)
        for item in results[:limit]
        if isinstance(item, dict)
    ]

  def _to_result(self, item: dict[str, Any], query: base.SourceQuery,
                 token: str) -> base.SourceResult:
    """Converts one Discogs search result into a SourceResult.

    Args:
      item: One entry from the search response's `results`.
      query: The original query, used to pick a track off the release.
      token: The API token, for the credits lookup.

    Returns:
      The converted candidate.
    """
    release_id = str(item.get("id", ""))
    artist, title = _split_release_title(item.get("title"))
    year = _as_int(item.get("year"))
    detail = self._release_detail(release_id, token)
    track = _matching_track(detail.get("tracklist"), query.title)
    # Remix credits live on the tracklist entry, not on the release: a
    # record holding four remixes credits each remixer against their own
    # track. Reading only release-level credits finds no remixer at all.
    credit_entries = (_release_credits_for(detail.get("extraartists"),
                                           track.get("position")) +
                      _as_entries(track.get("extraartists")))
    credits_by_field = _credits(credit_entries)

    tags = TrackTags(
        title=_clean_track_title(track.get("title")) or title,
        artist=artist,
        album=title if track else None,
        album_artist=artist,
        genre=_first(item.get("style")) or _first(item.get("genre")),
        remixer=credits_by_field.get("remixer"),
        composer=credits_by_field.get("composer"),
        lyricist=credits_by_field.get("lyricist"),
        mix_name=_mix_name(track.get("title")),
        year=year,
        track_number=position_number(track.get("position")),
    )
    return base.SourceResult(source=self.name,
                             source_id=release_id,
                             tags=tags,
                             art_url=item.get("cover_image"),
                             extra=_extra(item))

  def _release_detail(self, release_id: str, token: str) -> dict[str, Any]:
    """Fetches a release's tracklist and credits.

    Args:
      release_id: The Discogs release id.
      token: The API token.

    Returns:
      The release document, or an empty mapping if the lookup was
      skipped or failed. Losing credits must not lose the candidate.
    """
    if not self._fetch_credits or not release_id:
      return {}
    try:
      body = self._client.get_json(RELEASE_URL.format(release_id=release_id),
                                   headers=_auth(token))
    except http.HttpError:
      return {}
    return body if isinstance(body, dict) else {}


def _auth(token: str) -> dict[str, str]:
  """Builds the Discogs authorization header.

  Args:
    token: The personal access token.

  Returns:
    The header mapping.
  """
  return {"Authorization": f"Discogs token={token}"}


def _split_release_title(title: Any) -> tuple[str | None, str | None]:
  """Splits Discogs' "Artist - Title" release heading.

  Args:
    title: The `title` field from a search result.

  Returns:
    (artist, title). Either may be None.
  """
  if not isinstance(title, str) or not title:
    return (None, None)
  artist, separator, name = title.partition(" - ")
  if not separator:
    return (None, title.strip() or None)
  return (artist.strip() or None, name.strip() or None)


def _as_entries(extraartists: Any) -> list[dict[str, Any]]:
  """Returns an `extraartists` array as a list of credit entries.

  Args:
    extraartists: The raw array, or anything else.

  Returns:
    The well-formed entries, empty if there are none.
  """
  if not isinstance(extraartists, list):
    return []
  return [entry for entry in extraartists if isinstance(entry, dict)]


def _release_credits_for(extraartists: Any,
                         position: Any) -> list[dict[str, Any]]:
  """Keeps release-level credits that apply to one track.

  A release credit may name the tracks it covers. One that names other
  tracks must not be attributed to this one.

  Args:
    extraartists: The release's `extraartists` array.
    position: The matched track's position, e.g. "B1".

  Returns:
    The credits that apply to the whole release or to this track.
  """
  entries = []
  for entry in _as_entries(extraartists):
    tracks = str(entry.get("tracks") or "").strip()
    if not tracks or (position and str(position) in tracks):
      entries.append(entry)
  return entries


def _credits(entries: list[dict[str, Any]]) -> dict[str, str]:
  """Maps credited roles onto target fields.

  Args:
    entries: Credit entries from a release and its matched track.

  Returns:
    Field name to credited artist, for the roles we care about.
  """
  found: dict[str, list[str]] = {}
  for entry in entries:
    name = str(entry.get("name") or "").strip()
    role = str(entry.get("role") or "").strip().lower()
    if not name or not role:
      continue
    for field, roles in (("remixer", _REMIX_ROLES),
                         ("composer", _COMPOSER_ROLES), ("lyricist",
                                                         _LYRICIST_ROLES)):
      if any(candidate in role for candidate in roles):
        found.setdefault(field, []).append(name)
  return {
      field: ", ".join(dict.fromkeys(names)) for field, names in found.items()
  }


def _matching_track(tracklist: Any, wanted: str | None) -> dict[str, Any]:
  """Finds the track on a release that the query was about.

  A Discogs release is a whole record, so the fields that vary per track —
  title, position, mix name — have to be read off the right row.

  Args:
    tracklist: The release's `tracklist` array.
    wanted: The title being searched for.

  Returns:
    The matching track, the first track if nothing matches, or an empty
    mapping if there is no tracklist.
  """
  if not isinstance(tracklist, list):
    return {}
  tracks = [item for item in tracklist if isinstance(item, dict)]
  if not tracks:
    return {}
  if wanted:
    needle = wanted.strip().lower()
    for track in tracks:
      title = str(track.get("title") or "").strip().lower()
      if title and (needle in title or title in needle):
        return track
  return tracks[0]


def _clean_track_title(title: Any) -> str | None:
  """Strips a trailing mix name from a track title.

  Args:
    title: The track title as Discogs writes it.

  Returns:
    The title without its parenthesised mix, or None.
  """
  if not isinstance(title, str) or not title.strip():
    return None
  base_title, separator, _ = title.partition(" (")
  return (base_title if separator else title).strip() or None


def _mix_name(title: Any) -> str | None:
  """Extracts a parenthesised mix name from a track title.

  Args:
    title: The track title as Discogs writes it.

  Returns:
    The mix name, e.g. "LP Version", or None if there is none.
  """
  if not isinstance(title, str):
    return None
  _, separator, remainder = title.partition(" (")
  if not separator:
    return None
  mix, closed, _ = remainder.partition(")")
  return mix.strip() or None if closed else None


def position_number(position: Any) -> int | None:
  """Reads a track number from a Discogs position.

  Positions are "1" on CDs but "A2" on vinyl, where the letter is the
  side rather than part of the number.

  Args:
    position: The `position` field.

  Returns:
    The numeric part, or None if there is none.
  """
  digits = "".join(
      character for character in str(position or "") if character.isdigit())
  return int(digits) if digits else None


def _extra(item: dict[str, Any]) -> dict[str, str]:
  """Collects Discogs detail outside the target field list.

  Label and catalogue number are not tags this tool writes, but they are
  a large part of why Discogs leads for electronic music, so the probe
  shows them.

  Args:
    item: One search result.

  Returns:
    Label, catalogue number, country and styles, where present.
  """
  extra = {}
  label = _first(item.get("label"))
  if label:
    extra["label"] = label
  for key in ("catno", "country"):
    value = item.get(key)
    if value:
      extra[key] = str(value)
  styles = item.get("style")
  if isinstance(styles, list) and styles:
    extra["styles"] = ", ".join(str(style) for style in styles)
  return extra


def _first(value: Any) -> str | None:
  """Returns the first entry of a list field as text.

  Args:
    value: A list from the API, or anything else.

  Returns:
    The first entry, or None.
  """
  if isinstance(value, list) and value:
    return str(value[0])
  return None


def _as_int(value: Any) -> int | None:
  """Parses an integer without raising.

  Args:
    value: The value to parse.

  Returns:
    The integer, or None.
  """
  try:
    return int(str(value).strip())
  except (TypeError, ValueError):
    return None
