"""Finding audio files inside the configured source folders.

Every pass over the library — scanning, dedup, and later the restructure
pass — starts here, so the rule that nothing outside a configured source
folder is ever touched is enforced in one place rather than repeated at
each call site.
"""

import dataclasses
import pathlib
from typing import Iterator

from music_match.config.loader import SourceFolder
from music_match.config.loader import SourcesConfig

AUDIO_EXTENSIONS = frozenset({".m4a", ".mp3", ".wav", ".aiff", ".aif", ".flac"})

# Runtime output that may sit inside a source folder but is not library
# content and must never be scanned as if it were.
SKIPPED_DIRECTORIES = frozenset({"_review", ".music-match"})


@dataclasses.dataclass(frozen=True)
class LibraryFile:
  """One audio file, and the source folder it belongs to."""
  path: pathlib.Path
  source: SourceFolder


def is_audio(path: pathlib.Path) -> bool:
  """Returns whether a path looks like an audio file this tool handles.

  Args:
    path: The path to test.

  Returns:
    True if its extension is one of `AUDIO_EXTENSIONS`.
  """
  return path.suffix.lower() in AUDIO_EXTENSIONS


def walk_folder(
    folder: SourceFolder,
    skip: frozenset[str] = SKIPPED_DIRECTORIES) -> Iterator[pathlib.Path]:
  """Yields the audio files in one source folder, recursively.

  Args:
    folder: The source folder to walk.
    skip: Directory names to descend past.

  Yields:
    Paths to audio files, in sorted order so runs are reproducible.

  Raises:
    FileNotFoundError: If the folder does not exist. Source folders are
      matched by name and may legitimately be on a drive that is not
      mounted, which is worth reporting rather than silently scanning
      nothing.
  """
  if not folder.path.is_dir():
    raise FileNotFoundError(
        f"source folder '{folder.name}' is not at {folder.path}")
  for path in sorted(folder.path.rglob("*")):
    if not path.is_file() or not is_audio(path):
      continue
    if any(part in skip for part in path.relative_to(folder.path).parts[:-1]):
      continue
    yield path


def walk(sources: SourcesConfig,
         source_name: str | None = None) -> Iterator[LibraryFile]:
  """Yields the audio files across every configured source folder.

  Args:
    sources: The loaded source configuration.
    source_name: Restrict to one folder by name, or None for all.

  Yields:
    Each audio file with the folder it came from.

  Raises:
    KeyError: If `source_name` names a folder that is not configured.
    FileNotFoundError: If a folder to be scanned is not present.
  """
  if source_name is not None:
    folders = [sources.folders[source_name]]
  else:
    folders = list(sources.folders.values())
  for folder in folders:
    for path in walk_folder(folder):
      yield LibraryFile(path=path, source=folder)
