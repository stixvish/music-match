"""Reorganising a source folder into `Artist/Album/Track.ext`.

Deliberately the last pass, and deliberately separate from tagging.
Moving files on the strength of wrong metadata is far more annoying to
undo than fixing a wrong tag, so this runs only once the tags are
trusted — and it records every move it makes so it can be reversed.

Each source folder is reorganised *within itself*: `beatport/` and
`yt-dlp/` stay separate and are each cleaned internally. Nothing is ever
moved between them, and nothing leaves the configured folders.
"""

import dataclasses
import json
import pathlib
import re
import time
from typing import Any, Iterable

from music_match.library import LibraryFile
from music_match.tagging.fields import TrackTags

DEFAULT_MANIFEST_DIR = pathlib.Path(".music-match/restructure")

# Characters that cannot appear in a path component, or that cause
# trouble across the filesystems a library gets copied between.
_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f]')

# Most filesystems cap a single name at 255 bytes. Leaving room for a
# collision suffix and an extension keeps a long title from failing at
# the point of the move.
MAX_COMPONENT_LENGTH = 200

# Used when a tag is present but empty after sanitising.
UNKNOWN = "Unknown"


@dataclasses.dataclass(frozen=True)
class Move:
  """One planned or completed file move.

  Attributes:
    source: Where the file is now.
    destination: Where it should go, or None if it cannot be placed.
    reason: Why it cannot be placed, when destination is None.
  """
  source: pathlib.Path
  destination: pathlib.Path | None
  reason: str = ""

  def is_placeable(self) -> bool:
    """Returns whether this file has somewhere to go."""
    return self.destination is not None

  def is_noop(self) -> bool:
    """Returns whether the file is already where it belongs."""
    return self.destination is not None and self.destination == self.source


def sanitize(component: str) -> str:
  """Makes one path component safe to write to disk.

  Args:
    component: A raw tag value destined to become a folder or file name.

  Returns:
    The value with path separators and control characters removed,
    trailing dots and spaces trimmed, and length capped. Empty input
    becomes "Unknown" rather than an unnamed directory.
  """
  cleaned = _ILLEGAL.sub("_", component).strip()
  # A trailing dot or space is legal on macOS and not on Windows, and a
  # library gets copied between machines.
  cleaned = cleaned.rstrip(". ")
  if len(cleaned) > MAX_COMPONENT_LENGTH:
    cleaned = cleaned[:MAX_COMPONENT_LENGTH].rstrip(". ")
  return cleaned or UNKNOWN


def track_filename(tags: TrackTags, suffix: str) -> str:
  """Builds the file name for a track.

  Args:
    tags: The file's tags.
    suffix: The file extension, including the dot.

  Returns:
    "NN Title.ext", prefixed with the disc number when the release has
    more than one disc, so a two-disc album sorts correctly.
  """
  title = sanitize(tags.title or UNKNOWN)
  parts = []
  if tags.disc_number and (tags.disc_total or 0) > 1:
    parts.append(f"{tags.disc_number}-")
  if tags.track_number:
    parts.append(f"{tags.track_number:02d} ")
  prefix = "".join(parts)
  return f"{prefix}{title}{suffix}"


def target_for(path: pathlib.Path, tags: TrackTags,
               root: pathlib.Path) -> tuple[pathlib.Path | None, str]:
  """Works out where a file belongs.

  Args:
    path: The file's current location.
    tags: Its tags.
    root: The source folder it must stay inside.

  Returns:
    The destination and an empty reason, or None and why it cannot be
    placed. A file missing the tags that name a folder is left exactly
    where it is rather than filed under "Unknown".
  """
  artist = tags.album_artist or tags.artist
  missing = [
      name for name, value in (("artist", artist), ("album", tags.album),
                               ("title", tags.title)) if not value
  ]
  if missing:
    absent = ", ".join(missing)
    return (None, f"no {absent} tag")
  assert artist is not None and tags.album is not None
  return (root / sanitize(artist) / sanitize(tags.album) /
          track_filename(tags, path.suffix), "")


def plan(files: Iterable[tuple[LibraryFile, TrackTags]]) -> list[Move]:
  """Works out where every file should go.

  Args:
    files: Each library file with its tags.

  Returns:
    One move per file, including the ones that cannot be placed and the
    ones already in the right place.
  """
  moves = []
  for item, tags in files:
    destination, reason = target_for(item.path, tags, item.source.path)
    moves.append(Move(source=item.path, destination=destination, reason=reason))
  return moves


def free_destination(destination: pathlib.Path) -> pathlib.Path:
  """Finds a name that is not already taken.

  Two different recordings can legitimately share an artist, album and
  title — an album and its deluxe reissue, say — so a collision gets a
  suffix rather than overwriting.

  Args:
    destination: The preferred destination.

  Returns:
    A path that does not currently exist.

  Raises:
    FileExistsError: If no free name could be found.
  """
  if not destination.exists():
    return destination
  for suffix in range(1, 1000):
    candidate = destination.with_name(
        f"{destination.stem} ({suffix}){destination.suffix}")
    if not candidate.exists():
      return candidate
  raise FileExistsError(f"too many files named {destination.name}")


def write_manifest(
    moves: Iterable[tuple[pathlib.Path, pathlib.Path]],
    directory: pathlib.Path = DEFAULT_MANIFEST_DIR) -> pathlib.Path:
  """Records completed moves so they can be reversed.

  ARCHITECTURE's reason for running this pass last is that moving files
  on wrong metadata is hard to undo. Writing down what was moved is what
  makes it merely tedious instead.

  Args:
    moves: (from, to) pairs that were actually performed.
    directory: Where manifests are kept.

  Returns:
    The manifest path.

  Raises:
    OSError: If the manifest cannot be written.
  """
  directory.mkdir(parents=True, exist_ok=True)
  stamp = time.strftime("%Y%m%d-%H%M%S")
  path = directory / f"{stamp}.json"
  payload = [{
      "from": str(source),
      "to": str(destination)
  } for source, destination in moves]
  with path.open("w", encoding="utf-8") as handle:
    json.dump({"moves": payload}, handle, indent=1)
  return path


def read_manifest(
    path: pathlib.Path) -> list[tuple[pathlib.Path, pathlib.Path]]:
  """Reads back a manifest of completed moves.

  Args:
    path: The manifest file.

  Returns:
    (from, to) pairs, in the order they were performed.

  Raises:
    ValueError: If the file is not a manifest this module wrote.
  """
  try:
    with path.open("rb") as handle:
      document: dict[str, Any] = json.load(handle)
  except (OSError, json.JSONDecodeError) as err:
    raise ValueError(f"could not read manifest {path}: {err}") from err
  entries = document.get("moves")
  if not isinstance(entries, list):
    raise ValueError(f"{path} does not look like a restructure manifest")
  pairs = []
  for entry in entries:
    if not isinstance(entry, dict) or "from" not in entry or "to" not in entry:
      raise ValueError(f"{path} has a malformed entry")
    pairs.append((pathlib.Path(entry["from"]), pathlib.Path(entry["to"])))
  return pairs


def prune_empty(directory: pathlib.Path, stop_at: pathlib.Path) -> int:
  """Removes directories left empty by a move.

  Args:
    directory: Where the file was.
    stop_at: The source folder, which is never removed however empty.

  Returns:
    How many directories were removed.
  """
  removed = 0
  current = directory
  while current != stop_at and stop_at in current.parents:
    try:
      if any(current.iterdir()):
        break
      current.rmdir()
    except OSError:
      break
    removed += 1
    current = current.parent
  return removed
