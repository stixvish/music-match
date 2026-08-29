"""Dedup layer 3 — finding duplicates by audio fingerprint.

This is the final safety net, catching what the archive check (layer 1)
and the metadata pre-check (layer 2) missed: the same recording
downloaded twice under different titles, or held at two different
qualities.

Comparing every pair of fingerprints is not an option — a full
`similarity` scan costs roughly 10ms, so 2000 tracks would be about two
million pairs and several hours. Two cheap filters cut that down first:

1. **Shared sub-fingerprints.** An inverted index over the raw 32-bit
   sub-fingerprint values. Measured on this library, real duplicates
   share hundreds of values exactly (a lossless copy shares all 916; a
   128kbps transcode 718; a two-second-shifted re-encode 417) while
   unrelated tracks share none at all.
2. **Duration.** Two recordings of very different length are not
   duplicates. This one is about correctness, not just speed: because a
   similarity score is divided by the *shorter* fingerprint's length, a
   30-second snippet cut from a track would otherwise score close to
   1.0 against the full recording, and could then win on bitrate and
   displace it.

Only survivors of both get a full comparison.

The first filter carries an assumption worth stating: it finds duplicates
only if they share sub-fingerprints *exactly*. A copy that perturbed every
value by a single bit would score 1.0 on a full comparison and still never
be compared. Real transcodes are nowhere near that — they leave most
values untouched — but it is the reason `MIN_SHARED_SUBFINGERPRINTS` is
set well below what real duplicates share rather than tuned close to it.
"""

import collections
import dataclasses
import pathlib

from music_match.tagging import fingerprint as fp
from music_match.tagging.quality import AudioQuality

# A pair must share at least this many exact sub-fingerprints to be
# worth a full comparison. Set far below the hundreds real duplicates
# share, and far above the zero that unrelated tracks do.
MIN_SHARED_SUBFINGERPRINTS = 20

# Score at or above which two tracks are the same recording. Duplicates
# measured on real files score 0.95-1.0 and unrelated tracks 0.0, so
# there is a wide margin either side of this.
DEFAULT_SIMILARITY_THRESHOLD = 0.85

# Seconds two durations may differ by and still be compared.
DEFAULT_DURATION_TOLERANCE = 15.0


@dataclasses.dataclass(frozen=True)
class IndexedTrack:
  """A fingerprinted file, as dedup sees it.

  Attributes:
    track_id: The `tracks` row id.
    path: Where the file currently lives.
    fingerprint: Its raw chromaprint fingerprint.
    quality: Its audio quality, used to pick the keeper.
  """
  track_id: int
  path: pathlib.Path
  fingerprint: fp.Fingerprint
  quality: AudioQuality


@dataclasses.dataclass(frozen=True)
class Duplicate:
  """A track judged to be the same recording as its group's keeper."""
  track: IndexedTrack
  similarity: float


@dataclasses.dataclass(frozen=True)
class DuplicateGroup:
  """One recording held more than once.

  Attributes:
    keeper: The highest-quality copy, which stays where it is.
    duplicates: The lower-quality copies, best first.
  """
  keeper: IndexedTrack
  duplicates: tuple[Duplicate, ...]


def find_duplicates(
    tracks: list[IndexedTrack],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    duration_tolerance: float = DEFAULT_DURATION_TOLERANCE,
) -> list[DuplicateGroup]:
  """Groups tracks that are the same recording.

  Args:
    tracks: Every fingerprinted track to consider.
    threshold: Minimum similarity score to call two tracks the same
      recording.
    duration_tolerance: Seconds two durations may differ by and still be
      compared.

  Returns:
    One group per duplicated recording, each naming the copy to keep and
    the copies that lose. Tracks with no duplicate produce no group.
  """
  index = _build_index(tracks)
  grouped: set[int] = set()
  groups: list[DuplicateGroup] = []

  for position, track in enumerate(tracks):
    if position in grouped:
      continue
    matches = _matches_for(position, track, tracks, index, grouped, threshold,
                           duration_tolerance)
    if not matches:
      continue
    grouped.add(position)
    grouped.update(other for other, _ in matches)

    scored = [(position, 1.0)] + matches
    keeper_position = max(
        (item[0] for item in scored),
        key=lambda other:
        (tracks[other].quality.rank(), str(tracks[other].path)))
    keeper = tracks[keeper_position]
    # Scored against the keeper rather than against whichever track the
    # group happened to be built around, so the reported score always
    # means "how close this is to the copy being kept".
    duplicates = tuple(
        sorted((Duplicate(track=tracks[other],
                          similarity=fp.similarity(keeper.fingerprint,
                                                   tracks[other].fingerprint))
                for other, _ in scored
                if other != keeper_position),
               key=lambda item: (-item.similarity, str(item.track.path))))
    groups.append(DuplicateGroup(keeper=keeper, duplicates=duplicates))
  return groups


def _build_index(tracks: list[IndexedTrack]) -> dict[int, list[int]]:
  """Builds an inverted index from sub-fingerprint value to track position.

  Args:
    tracks: The tracks to index.

  Returns:
    Sub-fingerprint value to the positions of tracks containing it.
  """
  index: dict[int, list[int]] = collections.defaultdict(list)
  for position, track in enumerate(tracks):
    for value in set(track.fingerprint.values):
      index[value].append(position)
  return index


def _matches_for(
    position: int,
    track: IndexedTrack,
    tracks: list[IndexedTrack],
    index: dict[int, list[int]],
    grouped: set[int],
    threshold: float,
    duration_tolerance: float,
) -> list[tuple[int, float]]:
  """Finds every track that is the same recording as one given track.

  Args:
    position: The track's position in `tracks`.
    track: The track to match against.
    tracks: All tracks.
    index: The inverted sub-fingerprint index.
    grouped: Positions already claimed by an earlier group.
    threshold: Minimum similarity to count as a match.
    duration_tolerance: Seconds durations may differ by.

  Returns:
    (position, similarity) for each match, best first.
  """
  shared: collections.Counter[int] = collections.Counter()
  for value in set(track.fingerprint.values):
    for other in index.get(value, ()):
      if other > position and other not in grouped:
        shared[other] += 1

  matches: list[tuple[int, float]] = []
  for other, count in shared.items():
    if count < MIN_SHARED_SUBFINGERPRINTS:
      continue
    candidate = tracks[other]
    gap = abs(track.fingerprint.duration - candidate.fingerprint.duration)
    if gap > duration_tolerance:
      continue
    if track.fingerprint.algorithm != candidate.fingerprint.algorithm:
      continue
    score = fp.similarity(track.fingerprint, candidate.fingerprint)
    if score >= threshold:
      matches.append((other, score))
  matches.sort(key=lambda item: -item[1])
  return matches


def destination_for(track: IndexedTrack, source_name: str,
                    duplicates_root: pathlib.Path) -> pathlib.Path:
  """Chooses where a losing duplicate should be moved to.

  The path below the source folder is preserved so two duplicates with
  the same file name from different albums do not collide. The source
  folder is located by *name* within the track's own path rather than by
  comparing against the configured absolute path, so this still works
  when the library has moved to another drive. If something is already
  at the destination, a numeric suffix is added rather than overwriting.

  Args:
    track: The duplicate being moved.
    source_name: The name of the source folder the file lives under.
    duplicates_root: Where duplicates are collected.

  Returns:
    A path that does not currently exist.

  Raises:
    FileExistsError: If a free numbered name cannot be found.
  """
  candidate = duplicates_root / _relative_to_source(track.path, source_name)
  if not candidate.exists():
    return candidate
  for suffix in range(1, 1000):
    numbered = candidate.with_name(
        f"{candidate.stem} ({suffix}){candidate.suffix}")
    if not numbered.exists():
      return numbered
  raise FileExistsError(f"too many duplicates named {candidate.name}")


def _relative_to_source(path: pathlib.Path, source_name: str) -> pathlib.Path:
  """Returns the part of a path below its source folder.

  Args:
    path: The file's path.
    source_name: The name of the source folder to cut at.

  Returns:
    The path below the deepest component matching `source_name`, or just
    the file name if no component matches.
  """
  parts = path.parts
  for index in range(len(parts) - 2, -1, -1):
    if parts[index] == source_name:
      return pathlib.Path(*parts[index + 1:])
  return pathlib.Path(path.name)
