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


# --- artist credit formatting (SPEC.md §14) --------------------------------


@pytest.mark.parametrize(
  ("artists", "expected"),
  [
    (["Pitbull"], "Pitbull"),
    (["Lost Frequencies", "Calum Scott"], "Lost Frequencies & Calum Scott"),
    (
      ["David Guetta", "Bebe Rexha", "Brooks"],
      "David Guetta, Bebe Rexha & Brooks",
    ),
    (
      ["Metro Boomin", "A$AP Rocky", "Roisee", "Someone"],
      "Metro Boomin, A$AP Rocky, Roisee & Someone",
    ),
    ([], ""),
    (["", "  "], ""),
    (["A", "", "B"], "A & B"),
  ],
)
def test_format_artists(artists, expected):
  assert naming.format_artists(artists) == expected


def test_two_artists_collapse_to_ampersand():
  """The 2-artist rule falls out of the serial-comma rule, not a special case."""
  assert naming.format_artists(["A", "B"]) == "A & B"


def test_credit_order_is_preserved_not_sorted():
  assert naming.format_artists(["Zebra", "Aardvark"]) == "Zebra & Aardvark"


@pytest.mark.parametrize(
  ("raw", "expected"),
  [
    ("Alesso; Tove Lo", ["Alesso", "Tove Lo"]),
    ("Macklemore, Ryan Lewis", ["Macklemore", "Ryan Lewis"]),
    ("PARTYNEXTDOOR & Drake", ["PARTYNEXTDOOR", "Drake"]),
    (
      "Atif Aslam, Sunidhi Chauhan & Pritam",
      ["Atif Aslam", "Sunidhi Chauhan", "Pritam"],
    ),
    ("Pitbull", ["Pitbull"]),
    ("", []),
  ],
)
def test_split_artists_is_liberal_on_read(raw, expected):
  """Input tags are inconsistent; reading must accept every convention."""
  assert naming.split_artists(raw) == expected


def test_read_then_write_normalises_any_input_convention():
  for raw in ["Alesso; Tove Lo", "Alesso, Tove Lo", "Alesso & Tove Lo"]:
    assert naming.format_artists(naming.split_artists(raw)) == "Alesso & Tove Lo"


# --- version extraction (SPEC.md §14) --------------------------------------


@pytest.mark.parametrize(
  ("title", "mix_name", "remixer"),
  [
    # both: a person made a version
    ("Delilah [Tom Santa Remix]", "Tom Santa Remix", "Tom Santa"),
    ("Delilah (Tom Santa Remix)", "Tom Santa Remix", "Tom Santa"),
    ("I'm Good (Blue) - Brooks Remix", "Brooks Remix", "Brooks"),
    ("in plain sight (LEFTI REMIX)", "LEFTI REMIX", "LEFTI"),
    ("Make It - H.K.G Mix", "H.K.G Mix", "H.K.G"),
    # version only: no person named
    ("Say Nothing - Extended Mix", "Extended Mix", ""),
    ("Song (Radio Edit)", "Radio Edit", ""),
    ("Track (Original Mix)", "Original Mix", ""),
    ("Track (Club Mix)", "Club Mix", ""),
    ("Track (Instrumental Version)", "Instrumental Version", ""),
    # not a person: a year, a re-recording, a generic label
    ("I'm Not Alone (2019 Edit)", "2019 Edit", ""),
    ("Love Story (Taylor's Version)", "Taylor's Version", ""),
    ("Love Story (Taylor\u2019s Version)", "Taylor\u2019s Version", ""),
    # but a named remix keeps its remixer even with an ampersand
    (
      "Despacito (Major Lazer & MOSKA remix)",
      "Major Lazer & MOSKA remix",
      "Major Lazer & MOSKA",
    ),
    # neither
    ("Summer", "", ""),
    ("Mood (ft. iann dior)", "", ""),
    ("", "", ""),
  ],
)
def test_extract_version(title, mix_name, remixer):
  version = naming.extract_version(title)
  assert (version.mix_name, version.remixer) == (mix_name, remixer)


def test_mix_name_and_remixer_are_different_fields():
  """mix_name is which version; remixer is who made it (SPEC.md §14)."""
  extended = naming.extract_version("Track - Extended Mix")
  assert extended.mix_name and not extended.remixer

  remixed = naming.extract_version("Track [Someone Remix]")
  assert remixed.mix_name and remixed.remixer


def test_featured_clause_is_not_a_version():
  assert naming.extract_version("Mood (ft. iann dior)").mix_name == ""


def test_typographic_quotes_are_asciified():
  """MusicBrainz uses curly quotes; a search for "Can't" must match."""
  assert naming.canonical_title("Club Can’t Handle Me") == "Club Can't Handle Me"
  assert naming.safe_component("Flo’s Track") == "Flo's Track"
  assert naming.asciify_quotes("“quoted” – dash") == '"quoted" - dash'
