"""Output layout (tasks/todo.md t6, t26)."""

from pathlib import Path

from music import publish


def test_layout_is_family_then_artist():
  path = publish.layout_path(
    Path("/lib"), "electronic", "Fred again..", "Delilah (Tom Santa Remix)"
  )
  assert path == Path(
    "/lib/electronic/Fred again../Fred again.. - Delilah [Tom Santa Remix].aiff"
  )


def test_unknown_family_falls_back_to_other():
  path = publish.layout_path(Path("/lib"), "bollywood", "A", "B")
  assert path.parts[2] == "other"


def test_every_family_is_a_single_component():
  for family in publish.FAMILIES:
    assert "/" not in family
