"""Canonical naming (tasks/todo.md t26). Pure functions, 90% gate (SPEC.md §19)."""

import pytest

from music.publish import naming

CANONICAL = [
  # (raw, expected)
  ("Mood (feat. iann dior)", "Mood (ft. iann dior)"),
  ("Mood (ft. iann dior)", "Mood (ft. iann dior)"),
  ("Mood (Feat. iann dior)", "Mood (ft. iann dior)"),
  ("Mood featuring iann dior", "Mood (ft. iann dior)"),
  ("Delilah (Tom Santa Remix)", "Delilah [Tom Santa Remix]"),
  ("Delilah [Tom Santa Remix]", "Delilah [Tom Santa Remix]"),
  ("I'm Good (Blue) - Brooks Remix", "I'm Good (Blue) [Brooks Remix]"),
  ("Say Nothing - Extended Mix", "Say Nothing [Extended Mix]"),
  ("in plain sight (LEFTI REMIX)", "in plain sight [LEFTI REMIX]"),
  ("Summer", "Summer"),
  ("", ""),
  (
    "Everyday We Lit (Remix) (feat. Wiz Khalifa)",
    "Everyday We Lit (ft. Wiz Khalifa) [Remix]",
  ),
]


@pytest.mark.parametrize(("raw", "expected"), CANONICAL)
def test_canonical_title(raw, expected):
  assert naming.canonical_title(raw) == expected


def test_mix_designation_is_never_dropped():
  """Losing the remix name is worse than having no tag (SPEC.md §14)."""
  for raw, expected in CANONICAL:
    if "remix" in raw.lower():
      assert "[" in expected and "]" in expected


@pytest.mark.parametrize(
  ("raw", "expected"),
  [
    ("AC/DC", "AC-DC"),
    ("Drake: The Album", "Drake- The Album"),
    ("  padded  ", "padded"),
    ("", "unknown"),
    ("...", "unknown"),
    # a real artist in the library; the trailing dots must survive
    ("Fred again..", "Fred again.."),
  ],
)
def test_safe_component(raw, expected):
  assert naming.safe_component(raw) == expected


def test_safe_component_truncates():
  assert len(naming.safe_component("x" * 500)) == 120


def test_filename():
  assert (
    naming.filename("24kGoldn", "Mood (feat. iann dior)")
    == "24kGoldn - Mood (ft. iann dior).aiff"
  )
  assert (
    naming.filename("Fred again..", "Delilah (Tom Santa Remix)")
    == "Fred again.. - Delilah [Tom Santa Remix].aiff"
  )


def test_filename_is_path_safe():
  assert "/" not in naming.filename("AC/DC", "Back/Black")
