"""Query normalisation (tasks/todo.md t8).

Golden-file style: every row is a real string seen in the library or in the
resolution sample. A regression here would not crash anything — it would
quietly cost accuracy (SPEC.md §19), so the table is the guard.
"""

import pytest

from music import normalise

# (raw artist, raw title) -> (clean artist, clean title)
GOLDEN = [
  # --- measured failures from the 30-track musicbrainz sample (SPEC.md §4) ---
  (
    ("LMFAOVEVO", "LMFAO - Party Rock Anthem ft. Lauren Bennett, GoonRock"),
    ("LMFAO", "Party Rock Anthem"),
  ),
  (
    (
      "Calvin Harris",
      "Calvin Harris - I Need Your Love (Official Video) ft. Ellie Goulding",
    ),
    ("Calvin Harris", "I Need Your Love"),
  ),
  (
    (
      "DJ Khaled",
      "DJ Khaled - I'm The One ft. Justin Bieber, Quavo, Chance the Rapper",
    ),
    ("DJ Khaled", "I'm The One"),
  ),
  (("Burnie, Phoenix Ho", "Make It"), ("Burnie", "Make It")),
  (
    ("Rihanna", "Rihanna - Umbrella (Orange Version)"),
    ("Rihanna", "Umbrella (Orange Version)"),
  ),
  # --- real tags from the library ---
  (
    ("21 Savage; Metro Boomin", "Mr. Right Now (feat. Drake)"),
    ("21 Savage", "Mr. Right Now"),
  ),
  (("24kGoldn", "Mood (feat. iann dior)"), ("24kGoldn", "Mood")),
  (("Burnie - Topic", "Make It - H.K.G Mix"), ("Burnie", "Make It - H.K.G Mix")),
  # --- production noise ---
  (("Maroon_5", "Maroon_5_-_Sugar_Official_Music_Video"), ("Maroon 5", "Sugar")),
  (
    ("Ultra Records", "OMI - Cheerleader (Felix Jaehn Remix) [Official Video]"),
    ("Ultra Records", "OMI - Cheerleader (Felix Jaehn Remix)"),
  ),
  (("Kesha", "Ke$ha - Blow (Official Video)"), ("Kesha", "Ke$ha - Blow")),
  (("Drake", "Hotline Bling [Official Music Video]"), ("Drake", "Hotline Bling")),
  (("Artist", "Song (Remastered 2011)"), ("Artist", "Song")),
  # --- the mix designation must never be stripped: it is a different recording
  (
    ("Fred again..", "Delilah (Tom Santa Remix)"),
    ("Fred again..", "Delilah (Tom Santa Remix)"),
  ),
  (
    ("Khalid; LEFTI", "in plain sight (LEFTI REMIX)"),
    ("Khalid", "in plain sight (LEFTI REMIX)"),
  ),
  (
    ("David Guetta", "I'm Good (Blue) - Brooks Remix"),
    ("David Guetta", "I'm Good (Blue) - Brooks Remix"),
  ),
  # --- already clean, must pass through untouched ---
  (("Pitbull", "Time of Our Lives"), ("Pitbull", "Time of Our Lives")),
]


@pytest.mark.parametrize(("raw", "expected"), GOLDEN)
def test_golden(raw, expected):
  result = normalise.normalise(*raw)
  assert (result.artist, result.title) == expected


@pytest.mark.parametrize(
  ("raw", "expected"),
  [
    ("LMFAOVEVO", "LMFAO"),
    ("Burnie - Topic", "Burnie"),
    ("21 Savage; Metro Boomin", "21 Savage"),
    ("Calvin Harris & Dua Lipa", "Calvin Harris"),
    ("Tyler, The Creator", "Tyler"),  # known limitation, documented below
    ("", ""),
  ],
)
def test_clean_artist(raw, expected):
  assert normalise.clean_artist(raw) == expected


def test_featured_artists_are_extracted_not_just_discarded():
  result = normalise.normalise("DJ Khaled", "I'm The One ft. Justin Bieber, Quavo")
  assert result.featured == ("Justin Bieber", "Quavo")


def test_mix_designation_is_never_stripped():
  """Losing it would match the wrong recording entirely (SPEC.md §8)."""
  for title in [
    "Delilah (Tom Santa Remix)",
    "Say Nothing - Extended Mix",
    "in plain sight (LEFTI REMIX)",
    "Track (VIP Edit)",
  ]:
    cleaned = normalise.clean_title(title, "Artist")
    assert any(w in cleaned.lower() for w in ("remix", "mix", "edit"))


def test_empty_input_is_flagged_not_crashed():
  assert normalise.normalise("", "").is_empty
  assert normalise.normalise("Artist", "").is_empty


def test_title_only_falls_back_to_dash_split():
  result = normalise.normalise("", "LMFAO - Party Rock Anthem")
  assert (result.artist, result.title) == ("LMFAO", "Party Rock Anthem")
