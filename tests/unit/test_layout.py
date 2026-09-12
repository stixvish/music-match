"""Output layout (tasks/todo.md t6, t26; SPEC.md §14)."""

from pathlib import Path

from music import publish
from music.publish.naming import split_artists


def test_the_library_is_flat():
  path = publish.layout_path(Path("/lib"), "Fred again..", "Delilah (Tom Santa Remix)")
  assert path == Path("/lib/Fred again.. - Delilah [Tom Santa Remix].aiff")


def test_the_path_does_not_depend_on_genre():
  """Genre selects a precedence table; it is not a location.

  While it was also the folder name, correcting a genre meant moving the file
  — and every move costs a rekordbox or serato entry its cue points.
  """
  one = publish.layout_path(Path("/lib"), "An Artist", "A Song")
  assert one.parent == Path("/lib")


def test_a_collaboration_does_not_fragment_a_performer():
  """Why artist folders were rejected, measured on the real library.

  Arijit Singh appeared in ten distinct credits — including both
  `Antara Mitra & Arijit Singh` and `Arijit Singh & Antara Mitra` — so an
  artist-per-folder tree scatters one performer across ten directories.
  """
  billed = [
    "Arijit Singh",
    "Antara Mitra & Arijit Singh",
    "Arijit Singh & Antara Mitra",
    "Alka Yagnik & Arijit Singh",
  ]
  folders = set(billed)
  assert len(folders) == 4
  performers = {n for c in billed for n in split_artists(c)}
  assert "Arijit Singh" in performers
  # flat: one file per track, no directory per credit
  paths = {publish.layout_path(Path("/lib"), c, "Song").parent for c in billed}
  assert paths == {Path("/lib")}


def test_illegal_characters_are_still_sanitised():
  path = publish.layout_path(Path("/lib"), "AC/DC", "Back/Forth")
  assert path.parent == Path("/lib")
  assert "/" not in path.name.removesuffix(".aiff").replace(" - ", "")
