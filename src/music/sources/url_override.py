"""Resolve a track from a link the user pastes.

This is both a convenience and a **budget lever** (SPEC.md §9). The hardest
review items are remixes and edits that text search cannot find; pasting an
authoritative link turns a ~90-second puzzle into a ~10-second operation.

Parsing is pure and testable; the fetch that follows is not.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class Provider(StrEnum):
  """Where a pasted link points."""

  SPOTIFY = "spotify"
  MUSICBRAINZ = "musicbrainz"
  DISCOGS = "discogs"
  BEATPORT = "beatport"


@dataclass(frozen=True)
class Reference:
  """An identifier extracted from a pasted link."""

  provider: Provider
  kind: str
  identifier: str


_PATTERNS: tuple[tuple[Provider, str, re.Pattern[str]], ...] = (
  # open.spotify.com/track/ID, with or without a locale segment or query
  (
    Provider.SPOTIFY,
    "track",
    re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?track/([A-Za-z0-9]{22})"),
  ),
  (
    Provider.SPOTIFY,
    "album",
    re.compile(r"open\.spotify\.com/(?:[a-z-]+/)?album/([A-Za-z0-9]{22})"),
  ),
  # spotify:track:ID
  (Provider.SPOTIFY, "track", re.compile(r"spotify:track:([A-Za-z0-9]{22})")),
  (
    Provider.MUSICBRAINZ,
    "recording",
    re.compile(r"musicbrainz\.org/recording/([0-9a-f-]{36})"),
  ),
  (
    Provider.MUSICBRAINZ,
    "release",
    re.compile(r"musicbrainz\.org/release/([0-9a-f-]{36})"),
  ),
  (Provider.DISCOGS, "release", re.compile(r"discogs\.com/release/(\d+)")),
  (Provider.DISCOGS, "master", re.compile(r"discogs\.com/master/(\d+)")),
  (Provider.BEATPORT, "track", re.compile(r"beatport\.com/track/[^/]+/(\d+)")),
)


def parse(url: str) -> Reference | None:
  """Extract a provider reference from a pasted link.

  Args:
    url: Anything the user pasted.

  Returns:
    The reference, or None if the link is not recognised.
  """
  text = (url or "").strip()
  if not text:
    return None
  for provider, kind, pattern in _PATTERNS:
    found = pattern.search(text)
    if found:
      return Reference(provider, kind, found.group(1))
  return None


def is_supported(url: str) -> bool:
  """Whether a link can be resolved.

  Args:
    url: Anything the user pasted.

  Returns:
    True if a reference can be extracted.
  """
  return parse(url) is not None
