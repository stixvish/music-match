"""Genre-family routing (tasks/todo.md t23, SPEC.md §7).

Pure mapping only. Running the model needs ~20 MB of graphs and TensorFlow, so
inference is exercised by the nightly `live` job, not by CI (SPEC.md §19).
"""

import pytest

from music.classify import FAMILIES, Prediction, family_for


@pytest.mark.parametrize(
  ("genre", "family"),
  [
    ("Electronic", "electronic"),
    ("Hip Hop", "hip-hop"),
    ("Pop", "pop"),
    # rock and pop share a precedence table; splitting them buys nothing
    ("Rock", "pop"),
    ("Funk / Soul", "r&b-soul"),
    ("Latin", "world"),
    ("Reggae", "world"),
    ("Folk, World, & Country", "world"),
    # bollywood is filed inconsistently by discogs; both routes reach `world`
    ("Stage & Screen", "world"),
    ("Jazz", "other"),
    ("Classical", "other"),
    ("", "other"),
    ("Something Invented", "other"),
  ],
)
def test_family_for(genre, family):
  assert family_for(genre) == family


def test_family_matching_is_case_and_space_insensitive():
  assert family_for("  hip hop  ") == "hip-hop"
  assert family_for("ELECTRONIC") == "electronic"


def test_every_family_is_declared():
  for genre in ("Electronic", "Hip Hop", "Pop", "Funk / Soul", "Latin", "Jazz"):
    assert family_for(genre) in FAMILIES


def test_prediction_splits_genre_and_style():
  prediction = Prediction(label="Hip Hop---Trap", activation=0.52)
  assert prediction.genre == "Hip Hop"
  assert prediction.style == "Trap"
  assert prediction.family == "hip-hop"


def test_prediction_without_a_style():
  prediction = Prediction(label="Electronic", activation=0.9)
  assert prediction.genre == "Electronic"
  assert prediction.style == ""
  assert prediction.family == "electronic"


def test_empty_prediction():
  prediction = Prediction(label="", activation=0.0)
  assert prediction.genre == ""
  assert prediction.family == "other"


def test_style_is_available_but_is_not_a_tag_field():
  """Style is diagnostic only — it must not appear in the ID3 frame map."""
  from music.publish.tag import FRAME_MAP

  assert "style" not in FRAME_MAP


# --- isrc country override (cp5) -------------------------------------------


def test_indian_isrc_overrides_a_misclassification():
  """cp5: the model has no Indian class and guessed K-pop, Laiko, Pachanga.

  An ISRC registrant country is a reliable signal where the classifier has no
  category at all.
  """
  from music.classify import family_for_identity

  # the classifier said Pop; the ISRC says India
  assert family_for_identity("Pop", "INU671800123") == "world"
  assert family_for_identity("Electronic", "IN-U67-18-00123") == "world"


def test_non_regional_isrc_leaves_the_classifier_alone():
  from music.classify import family_for_identity

  assert family_for_identity("Electronic", "GBDUW0000053") == "electronic"
  assert family_for_identity("Hip Hop", "USQX92003025") == "hip-hop"


def test_missing_isrc_falls_back_to_the_classifier():
  from music.classify import family_for_identity

  assert family_for_identity("Pop", None) == "pop"
  assert family_for_identity("Pop", "") == "pop"
  assert family_for_identity("Pop", "X") == "pop"
