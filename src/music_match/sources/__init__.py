"""Metadata sources, one module per public database.

`SOURCE_TYPES` is the registry the probe and, later, the matcher work
from. Adding a source means writing a `MetadataSource` subclass and adding
it here; nothing else needs to know it exists.
"""

from typing import Callable, Mapping

from music_match.sources.base import MetadataSource
from music_match.sources.base import SourceError
from music_match.sources.base import SourceQuery
from music_match.sources.base import SourceResult
from music_match.sources.discogs import DiscogsSource
from music_match.sources.itunes import ITunesSource
from music_match.sources.musicbrainz import MusicBrainzSource
from music_match.sources.spotify import SpotifySource

SOURCE_TYPES: Mapping[str, Callable[[], MetadataSource]] = {
    DiscogsSource.name: DiscogsSource,
    MusicBrainzSource.name: MusicBrainzSource,
    SpotifySource.name: SpotifySource,
    ITunesSource.name: ITunesSource,
}

__all__ = [
    "MetadataSource",
    "SOURCE_TYPES",
    "SourceError",
    "SourceQuery",
    "SourceResult",
    "build",
    "build_all",
    "known_names",
]


def known_names() -> tuple[str, ...]:
  """Returns every registered source name, in probe display order."""
  return tuple(SOURCE_TYPES)


def build(name: str) -> MetadataSource:
  """Creates one source by name.

  Args:
    name: The source's registered name.

  Returns:
    A new instance.

  Raises:
    KeyError: If no source is registered under that name.
  """
  return SOURCE_TYPES[name]()


def build_all(names: tuple[str, ...] | None = None) -> list[MetadataSource]:
  """Creates every registered source, or a named subset.

  Args:
    names: Which sources to build, or None for all of them.

  Returns:
    The instances, in registry order.

  Raises:
    KeyError: If a requested name is not registered.
  """
  wanted = names or known_names()
  return [build(name) for name in wanted]
