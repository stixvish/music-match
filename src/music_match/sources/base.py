"""The interface every metadata source implements.

Adding a source means implementing `MetadataSource` and registering it in
`sources/__init__.py`. Results come back as the same `TrackTags` the
tagging layer uses, so the probe and, later, the matcher can compare
sources field by field without knowing anything about any one API.
"""

import abc
import dataclasses
from typing import Mapping

from music_match.sources import http
from music_match.tagging.fields import TrackTags


class SourceError(Exception):
  """Raised when a source cannot answer a query."""


@dataclasses.dataclass(frozen=True)
class SourceQuery:
  """What is known about a track before any source has been asked.

  Attributes:
    title: The track title, the one field a search really needs.
    artist: The performing artist, if known.
    album: The album, if known.
    duration_seconds: The file's duration, used to rank candidates.
  """
  title: str | None = None
  artist: str | None = None
  album: str | None = None
  duration_seconds: float | None = None

  @classmethod
  def from_tags(cls,
                tags: TrackTags,
                duration_seconds: float | None = None) -> "SourceQuery":
    """Builds a query from a file's existing tags.

    Existing tags are treated as a search hint, however partial — this is
    the same path a well-tagged Beatport file and a bare yt-dlp download
    both take.

    Args:
      tags: The file's current tags.
      duration_seconds: The file's duration, if known.

    Returns:
      The query.
    """
    return cls(title=tags.title,
               artist=tags.artist or tags.album_artist,
               album=tags.album,
               duration_seconds=duration_seconds)

  def is_usable(self) -> bool:
    """Returns whether there is enough here to search on."""
    return bool(self.title and self.title.strip())

  def as_text(self) -> str:
    """Returns the query as a single free-text search string.

    Returns:
      Artist and title joined, suitable for APIs that take one box.
    """
    return " ".join(part for part in (self.artist, self.title) if part)


@dataclasses.dataclass(frozen=True)
class SourceResult:
  """One candidate a source returned.

  Attributes:
    source: The source's name, e.g. "discogs".
    source_id: That source's own identifier for this release or recording.
    tags: The metadata, in the same shape the tagging layer writes.
    art_url: A cover image URL, preferring the largest the source offers.
    art_size: The width in pixels of `art_url`, if the source says.
    duration_seconds: The candidate's playing time, where the source
      reports one. Compared against the file's own duration, this is what
      separates a studio cut from a live version that matches on every
      string field.
    extra: Source-specific detail worth showing in a probe but outside the
      target field list — Discogs label and catalogue number, say.
  """
  source: str
  source_id: str
  tags: TrackTags
  art_url: str | None = None
  art_size: int | None = None
  duration_seconds: float | None = None
  extra: Mapping[str, str] = dataclasses.field(default_factory=dict)


class MetadataSource(abc.ABC):
  """A public database that can be asked about a track."""

  #: Short lowercase identifier, matching the names used in
  #: precedence.toml.
  name: str = ""

  def __init__(self, client: http.HttpClient) -> None:
    """Stores the HTTP client this source makes its requests through.

    Owned by the base class so a caller can reach it without knowing
    which subclass it holds.

    Args:
      client: The configured client, carrying this source's rate limit.
    """
    self._client = client

  @property
  def client(self) -> http.HttpClient:
    """Returns the HTTP client this source uses."""
    return self._client

  def disable_cache(self) -> None:
    """Stops this source reading from or writing to the response cache.

    Used by `probe --no-cache`, whose whole purpose is checking whether a
    source's data has actually changed — which a cached answer would
    hide.
    """
    self._client.cache = None

  @abc.abstractmethod
  def is_available(self) -> bool:
    """Returns whether this source has the credentials it needs."""

  @abc.abstractmethod
  def search(self, query: SourceQuery, limit: int = 3) -> list[SourceResult]:
    """Finds candidate matches for a track.

    Args:
      query: What is known about the track.
      limit: The most candidates to return.

    Returns:
      Candidates, best first by the source's own ranking. Empty if the
      source knows nothing about it.

    Raises:
      SourceError: If the source could not be reached or answered
        unusably.
    """
