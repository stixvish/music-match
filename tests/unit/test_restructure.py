"""Tests for reorganising a source folder into Artist/Album/Track."""

import json
import pathlib

import pytest

from music_match import restructure
from music_match.config import loader
from music_match.library import LibraryFile
from music_match.tagging.fields import TrackTags

ROOT = pathlib.Path("/music/yt-dlp")


def tags(**kwargs: object) -> TrackTags:
  """Builds tags with sensible defaults for a placeable file.

  Args:
    **kwargs: Fields to override.

  Returns:
    The tags.
  """
  base = {
      "title": "Strobe",
      "artist": "deadmau5",
      "album": "For Lack of a Better Name",
      "track_number": 9,
  }
  base.update(kwargs)
  return TrackTags(**base)  # type: ignore[arg-type]


def target(**kwargs: object) -> pathlib.Path | None:
  """Returns where a file with these tags would go.

  Args:
    **kwargs: Fields to override.

  Returns:
    The destination, or None if unplaceable.
  """
  return restructure.target_for(ROOT / "x.m4a", tags(**kwargs), ROOT)[0]


def test_a_tagged_file_gets_an_artist_album_track_path() -> None:
  """The layout ARCHITECTURE specifies."""
  assert target() == (ROOT / "deadmau5" / "For Lack of a Better Name" /
                      "09 Strobe.m4a")


def test_the_album_artist_wins() -> None:
  """Otherwise a compilation scatters across a folder per guest."""
  assert target(artist="Guest", album_artist="Various Artists",
                album="Comp") == (ROOT / "Various Artists" / "Comp" /
                                  "09 Strobe.m4a")


def test_a_multi_disc_release_keeps_its_disc_number() -> None:
  """Otherwise track 1 of disc 2 sorts above track 2 of disc 1."""
  destination = target(disc_number=2, disc_total=2, track_number=4)
  assert destination is not None
  assert destination.name == "2-04 Strobe.m4a"


def test_a_single_disc_release_does_not() -> None:
  """A disc number on a single-disc album is noise."""
  destination = target(disc_number=1, disc_total=1)
  assert destination is not None
  assert destination.name == "09 Strobe.m4a"


def test_a_track_without_a_number_is_still_placed() -> None:
  """A missing track number is not a reason to leave a file behind."""
  destination = target(track_number=None)
  assert destination is not None
  assert destination.name == "Strobe.m4a"


def test_the_extension_is_preserved() -> None:
  """WAV files stay WAV; this pass moves files, it does not convert."""
  destination, _ = restructure.target_for(ROOT / "x.wav", tags(), ROOT)
  assert destination is not None
  assert destination.suffix == ".wav"


@pytest.mark.parametrize("field", ["artist", "album", "title"])
def test_a_file_missing_a_naming_tag_is_left_alone(field: str) -> None:
  """Filing an untagged file under "Unknown" buries it.

  Leaving it exactly where it is keeps it findable, and the reason is
  reported so it can be fixed.
  """
  destination, reason = restructure.target_for(ROOT / "x.m4a",
                                               tags(**{field: None}), ROOT)
  assert destination is None
  assert field in reason


def test_path_separators_are_sanitised() -> None:
  """"AC/DC" must not become two directories.

  This is the one that quietly corrupts a library: an unsanitised slash
  creates a folder nobody meant to make.
  """
  destination = target(artist="AC/DC", album="The Razors Edge")
  assert destination is not None
  assert destination.parent.parent.name == "AC_DC"
  assert destination.relative_to(ROOT).parts[0] == "AC_DC"


@pytest.mark.parametrize("raw,expected", [
    ("AC/DC", "AC_DC"),
    ("Artist: The Album", "Artist_ The Album"),
    ("Trailing dot.", "Trailing dot"),
    ("  spaced  ", "spaced"),
    ("", "Unknown"),
    ("...", "Unknown"),
])
def test_sanitize_handles_awkward_names(raw: str, expected: str) -> None:
  """Names travel between filesystems with different rules."""
  assert restructure.sanitize(raw) == expected


def test_long_names_are_capped() -> None:
  """Most filesystems refuse a component over 255 bytes."""
  assert len(restructure.sanitize("x" *
                                  500)) <= restructure.MAX_COMPONENT_LENGTH


def test_a_file_already_in_place_is_a_noop() -> None:
  """Re-running the pass should not churn the library."""
  destination = target()
  assert destination is not None
  move = restructure.Move(source=destination, destination=destination)
  assert move.is_noop()


def source_folder(root: pathlib.Path) -> loader.SourceFolder:
  """Builds a source folder rooted at a path.

  Args:
    root: The folder path.

  Returns:
    The configured folder.
  """
  return loader.SourceFolder(name="yt-dlp",
                             path=root,
                             check_for_video_rips=True)


def test_plan_covers_every_file(tmp_path: pathlib.Path) -> None:
  """Placeable, already-placed and unplaceable files all come back."""
  folder = source_folder(tmp_path)
  files = [
      (LibraryFile(path=tmp_path / "a.m4a", source=folder), tags()),
      (LibraryFile(path=tmp_path / "b.m4a", source=folder), tags(album=None)),
  ]
  planned = restructure.plan(files)
  assert len(planned) == 2
  assert planned[0].is_placeable()
  assert not planned[1].is_placeable()


def test_a_collision_gets_a_suffix(tmp_path: pathlib.Path) -> None:
  """Two recordings can share an artist, album and title."""
  taken = tmp_path / "09 Strobe.m4a"
  taken.write_bytes(b"x")
  assert restructure.free_destination(taken).name == "09 Strobe (1).m4a"


def test_a_free_name_is_left_alone(tmp_path: pathlib.Path) -> None:
  """No suffix is added when nothing is in the way."""
  wanted = tmp_path / "09 Strobe.m4a"
  assert restructure.free_destination(wanted) == wanted


def test_a_manifest_round_trips(tmp_path: pathlib.Path) -> None:
  """The record of what moved is what makes this reversible."""
  moves = [(tmp_path / "old.m4a", tmp_path / "new" / "01 New.m4a")]
  manifest = restructure.write_manifest(moves, tmp_path / "manifests")
  assert restructure.read_manifest(manifest) == moves


def test_a_malformed_manifest_is_refused(tmp_path: pathlib.Path) -> None:
  """Reversing moves from a file of unknown shape would be reckless."""
  path = tmp_path / "bad.json"
  path.write_text(json.dumps({"moves": [{"from": "only"}]}), encoding="utf-8")
  with pytest.raises(ValueError, match="malformed"):
    restructure.read_manifest(path)


def test_an_unreadable_manifest_is_refused(tmp_path: pathlib.Path) -> None:
  """A file that is not JSON is reported clearly."""
  path = tmp_path / "bad.json"
  path.write_text("not json", encoding="utf-8")
  with pytest.raises(ValueError):
    restructure.read_manifest(path)


def test_empty_directories_are_pruned(tmp_path: pathlib.Path) -> None:
  """A move leaves its old folders behind unless they are cleaned up."""
  nested = tmp_path / "Artist" / "Album"
  nested.mkdir(parents=True)
  assert restructure.prune_empty(nested, tmp_path) == 2
  assert not (tmp_path / "Artist").exists()


def test_pruning_stops_at_the_source_folder(tmp_path: pathlib.Path) -> None:
  """The source folder itself is never removed, however empty."""
  restructure.prune_empty(tmp_path, tmp_path)
  assert tmp_path.exists()


def test_pruning_leaves_a_folder_that_still_has_files(
    tmp_path: pathlib.Path) -> None:
  """Only genuinely empty directories go."""
  nested = tmp_path / "Artist" / "Album"
  nested.mkdir(parents=True)
  (nested / "keep.m4a").write_bytes(b"x")
  assert restructure.prune_empty(nested, tmp_path) == 0
  assert nested.exists()
