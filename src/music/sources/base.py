"""The one interface every metadata source implements.

Arbitration never knows which source it is talking to: it receives candidates
and ranks them. A broken source returns an empty list and the pipeline
continues, which is why Beatport — the only source with no official api, and
the one most likely to break — is isolated behind this (SPEC.md §7).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

# fields a source may supply. album fields are resolved as a group from one
# release, never mixed across releases (SPEC.md §7).
ALBUM_GROUP = ("album", "album_artist", "track_number", "disc_number")


class FieldCandidate(BaseModel):
  """One value one source offered for one field."""

  field: str
  value: str
  source: str
  confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ReleaseInfo(BaseModel):
  """A release a recording appears on, for release selection (SPEC.md §7)."""

  release_id: str
  title: str
  track_count: int = 0
  track_number: int | None = None
  disc_number: int | None = None
  album_artist: str = ""
  date: str = ""
  primary_type: str = ""
  secondary_types: tuple[str, ...] = ()

  @property
  def is_album(self) -> bool:
    """Whether this is a plain studio album, not a compilation or live record."""
    excluded = {"Compilation", "Live", "Remix", "DJ-mix", "Mixtape/Street"}
    return self.primary_type == "Album" and not (set(self.secondary_types) & excluded)


@dataclass(frozen=True)
class Identity:
  """What we know about a track when asking a source about it."""

  artist: str
  title: str
  duration_s: float | None = None
  isrc: str | None = None
  mb_recording_id: str | None = None
  # full credit with collaborators, for a retry when the primary artist misses
  artist_full: str = ""


class Source(Protocol):
  """Every adapter satisfies this and nothing more."""

  name: str

  def lookup(self, identity: Identity) -> Sequence[FieldCandidate]:
    """Return whatever this source knows about the track.

    Args:
      identity: What we know so far.

    Returns:
      Candidates, possibly empty. Never raises for a simple miss.
    """
    ...
