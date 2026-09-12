"""The one interface every metadata source implements.

Arbitration never knows which source it is talking to: it receives candidates
and ranks them. A broken source returns an empty list and the pipeline
continues, which is why Beatport — the only source with no official api, and
the one most likely to break — is isolated behind this (SPEC.md §7).
"""

from collections.abc import Callable, Sequence
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
  release_group_id: str = ""
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


def best_result[T](
  identity: Identity,
  results: Sequence[T],
  *,
  artist: Callable[[T], str],
  title: Callable[[T], str],
) -> T | None:
  """Pick the result that best renders the identity we searched for.

  Every text source is asked for several results and used to keep only the
  first. Position one is frequently a remaster, a live cut or a re-recording
  with the original further down — iTunes returned `Love Story (Taylor's
  Version)` ahead of `Love Story`, and `Baby (feat. Ludacris)` ahead of
  `Beauty and a Beat` — so the other four were paid for and discarded
  unexamined (SPEC.md §12).

  Args:
    identity: What we searched for.
    results: The source's raw results, in its own order.
    artist: Reads the artist name out of one result.
    title: Reads the title out of one result.

  Returns:
    The best result, or None when there are none. Ties keep the source's own
    ordering, so a source that already ranks well is never made worse.
  """
  from music.identify import match_score

  if not results:
    return None
  scored = [
    (match_score(identity.artist, identity.title, artist(r), title(r)), -index, r)
    for index, r in enumerate(results)
  ]
  return max(scored, key=lambda item: (item[0], item[1]))[2]
