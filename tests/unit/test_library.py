"""Tests for finding audio files inside configured source folders."""

import pathlib

import pytest

from music_match import library
from music_match.config import loader


def build_library(root: pathlib.Path) -> loader.SourcesConfig:
  """Creates a two-folder library on disk and the config describing it.

  Args:
    root: Directory to build the library under.

  Returns:
    The matching source configuration.
  """
  for relative in (
      "yt-dlp/a.m4a",
      "yt-dlp/Album/b.mp3",
      "yt-dlp/notes.txt",
      "yt-dlp/_review/possible-video-rip/c.m4a",
      "yt-dlp/.music-match/art-store/d.m4a",
      "beatport/e.wav",
      "outside/f.m4a",
  ):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
  return loader.SourcesConfig(folders={
      "yt-dlp":
          loader.SourceFolder(name="yt-dlp",
                              path=root / "yt-dlp",
                              check_for_video_rips=True),
      "beatport":
          loader.SourceFolder(name="beatport",
                              path=root / "beatport",
                              check_for_video_rips=False),
  },
                              duplicates_path=root / "dupes")


def test_walk_finds_audio_recursively(tmp_path: pathlib.Path) -> None:
  """Audio files are found at any depth inside a source folder."""
  sources = build_library(tmp_path)
  found = {item.path.name for item in library.walk(sources)}
  assert found == {"a.m4a", "b.mp3", "e.wav"}


def test_walk_ignores_non_audio(tmp_path: pathlib.Path) -> None:
  """Files that are not audio are not yielded."""
  sources = build_library(tmp_path)
  assert all(item.path.suffix != ".txt" for item in library.walk(sources))


def test_walk_never_leaves_the_source_folders(tmp_path: pathlib.Path) -> None:
  """Nothing outside a configured folder is ever returned."""
  sources = build_library(tmp_path)
  assert all("outside" not in item.path.parts for item in library.walk(sources))


def test_walk_skips_runtime_output(tmp_path: pathlib.Path) -> None:
  """Quarantine and art-store directories are not library content."""
  sources = build_library(tmp_path)
  names = {item.path.name for item in library.walk(sources)}
  assert "c.m4a" not in names
  assert "d.m4a" not in names


def test_walk_tags_each_file_with_its_source(tmp_path: pathlib.Path) -> None:
  """Each file knows which configured folder it came from."""
  sources = build_library(tmp_path)
  by_name = {item.path.name: item.source.name for item in library.walk(sources)}
  assert by_name["a.m4a"] == "yt-dlp"
  assert by_name["e.wav"] == "beatport"


def test_walk_can_be_limited_to_one_source(tmp_path: pathlib.Path) -> None:
  """A named source restricts the walk to that folder."""
  sources = build_library(tmp_path)
  found = {item.path.name for item in library.walk(sources, "beatport")}
  assert found == {"e.wav"}


def test_walk_rejects_an_unknown_source(tmp_path: pathlib.Path) -> None:
  """Naming a folder that is not configured is an error."""
  sources = build_library(tmp_path)
  with pytest.raises(KeyError):
    list(library.walk(sources, "nope"))


def test_walk_reports_a_missing_folder(tmp_path: pathlib.Path) -> None:
  """An unmounted drive is reported, not silently scanned as empty."""
  sources = build_library(tmp_path)
  missing = loader.SourcesConfig(folders={
      "gone":
          loader.SourceFolder(name="gone",
                              path=tmp_path / "gone",
                              check_for_video_rips=False)
  },
                                 duplicates_path=tmp_path / "dupes")
  assert list(library.walk(sources, "yt-dlp"))
  with pytest.raises(FileNotFoundError, match="gone"):
    list(library.walk(missing))


def test_walk_is_sorted(tmp_path: pathlib.Path) -> None:
  """Repeated runs see files in the same order."""
  sources = build_library(tmp_path)
  first = [item.path for item in library.walk(sources)]
  assert first == sorted(first[:2]) + first[2:]


@pytest.mark.parametrize("name,expected", [
    ("track.m4a", True),
    ("track.MP3", True),
    ("track.flac", True),
    ("track.aiff", True),
    ("track.txt", False),
    ("track", False),
])
def test_is_audio(name: str, expected: bool) -> None:
  """Extension matching is case-insensitive and covers the real formats."""
  assert library.is_audio(pathlib.Path(name)) is expected
