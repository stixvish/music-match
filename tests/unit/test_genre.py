"""Tests for local genre detection.

Essentia is an optional install and the models are 20MB of gitignored
binaries, so nothing here loads either. The module is deliberately split
so that everything above the Essentia boundary — aggregating frames,
ranking labels, reading model metadata, checking what is installed — is
pure Python and testable on its own.
"""

import json
import pathlib

import pytest

from music_match.config import loader
from music_match.tagging import genre


def test_mean_over_frames_averages_each_label() -> None:
  """Per-frame scores collapse to one score per label."""
  assert genre.mean_over_frames([[0.0, 1.0], [1.0, 0.0]]) == [0.5, 0.5]


def test_mean_over_frames_of_nothing_is_empty() -> None:
  """A file too short to score a frame yields no scores, not a crash."""
  assert genre.mean_over_frames([]) == []


def test_mean_over_frames_rejects_ragged_input() -> None:
  """Frames of differing width mean the model output is not what we think."""
  with pytest.raises(genre.GenreError, match="differing width"):
    genre.mean_over_frames([[0.1, 0.2], [0.3]])


def test_one_odd_frame_does_not_decide_the_answer() -> None:
  """Averaging is what stops an atypical intro deciding the genre."""
  frames = [[1.0, 0.0]] + [[0.0, 1.0]] * 9
  scores = genre.mean_over_frames(frames)
  assert scores[1] > scores[0]


def test_rank_predictions_orders_by_confidence() -> None:
  """The strongest label comes first."""
  ranked = genre.rank_predictions([0.1, 0.9, 0.5],
                                  ["Blues---A", "Electronic---B", "Pop---C"])
  assert [item.label for item in ranked
         ] == ["Electronic---B", "Pop---C", "Blues---A"]


def test_rank_predictions_respects_the_limit() -> None:
  """Only the requested number of predictions is kept."""
  ranked = genre.rank_predictions([0.1, 0.9, 0.5], ["a", "b", "c"], limit=2)
  assert len(ranked) == 2


def test_rank_predictions_breaks_ties_stably() -> None:
  """Equal scores order by label so repeated runs agree."""
  ranked = genre.rank_predictions([0.5, 0.5], ["Rock---B", "Blues---A"])
  assert [item.label for item in ranked] == ["Blues---A", "Rock---B"]


def test_rank_predictions_rejects_a_length_mismatch() -> None:
  """A score count that does not match the label count is an error."""
  with pytest.raises(genre.GenreError, match="scores for"):
    genre.rank_predictions([0.1, 0.2], ["only-one"])


def test_prediction_splits_genre_and_style() -> None:
  """Discogs labels carry a genre and a style, separated by ---."""
  prediction = genre.Prediction(label="Electronic---Deep House", confidence=0.9)
  assert prediction.genre == "Electronic"
  assert prediction.style == "Deep House"


def test_prediction_without_a_style() -> None:
  """A bare genre has no style rather than an empty one."""
  prediction = genre.Prediction(label="Electronic", confidence=0.9)
  assert prediction.genre == "Electronic"
  assert prediction.style is None


def test_result_exposes_the_strongest_label() -> None:
  """The result reports its own top prediction."""
  result = genre.GenreResult(predictions=(
      genre.Prediction(label="Hip Hop---Trap", confidence=0.8),
      genre.Prediction(label="Pop---K-pop", confidence=0.2),
  ))
  assert result.label == "Hip Hop---Trap"
  assert result.top is not None
  assert result.is_confident(0.5)
  assert not result.is_confident(0.9)


def test_empty_result_has_no_label() -> None:
  """A file with no predictions reports None rather than raising."""
  result = genre.GenreResult(predictions=())
  assert result.label is None
  assert result.top is None
  assert not result.is_confident(0.0)


# Every top-level genre the discogs-effnet model can emit. Hard-coded
# rather than read from the model metadata, which is a gitignored 20MB
# download CI does not have.
TAXONOMY = (
    "Blues",
    "Brass & Military",
    "Children\'s",
    "Classical",
    "Electronic",
    "Folk, World, & Country",
    "Funk / Soul",
    "Hip Hop",
    "Jazz",
    "Latin",
    "Non-Music",
    "Pop",
    "Reggae",
    "Rock",
    "Stage & Screen",
)


def test_detected_labels_map_onto_precedence_keys() -> None:
  """The model's vocabulary lines up with precedence.toml's keys.

  This is the whole reason for choosing discogs-effnet: what it emits and
  what the config is keyed on are the same taxonomy.
  """
  assert loader.normalize_genre("Electronic---Deep House") == "electronic"
  assert loader.normalize_genre("Hip Hop---Cloud Rap") == "hip_hop"
  assert loader.normalize_genre("Funk / Soul---Rhythm & Blues") == "funk_soul"


@pytest.mark.parametrize("label", TAXONOMY)
def test_every_taxonomy_genre_yields_a_clean_key(label: str) -> None:
  """No real label produces a doubled, leading or trailing underscore.

  The punctuation in this vocabulary — slashes, ampersands, commas and an
  apostrophe — is exactly what a naive normalizer mangles.
  """
  key = loader.normalize_genre(label)
  assert key
  assert key == key.strip("_")
  assert "__" not in key
  assert all(part.isalnum() and part.islower() or part.isdigit()
             for part in key.split("_"))


def test_taxonomy_keys_are_distinct() -> None:
  """Two different genres must never collapse onto the same key."""
  keys = [loader.normalize_genre(label) for label in TAXONOMY]
  assert len(set(keys)) == len(keys)


def test_style_does_not_change_the_key() -> None:
  """Every style under a genre maps to that genre's single key."""
  assert (loader.normalize_genre("Electronic---Deep House") ==
          loader.normalize_genre("Electronic---Drum n Bass"))


def write_metadata(tmp_path: pathlib.Path, document: object) -> pathlib.Path:
  """Writes a classifier metadata file.

  Args:
    tmp_path: The temporary directory.
    document: The JSON document to write.

  Returns:
    Path to the written file.
  """
  path = tmp_path / genre.CLASSIFIER_METADATA_FILE
  path.write_text(json.dumps(document), encoding="utf-8")
  return path


def test_load_classes_reads_the_label_list(tmp_path: pathlib.Path) -> None:
  """Labels come back in the order the model outputs them."""
  path = write_metadata(tmp_path, {"classes": ["Blues---A", "Rock---B"]})
  assert genre.load_classes(path) == ("Blues---A", "Rock---B")


def test_load_classes_reports_a_missing_file(tmp_path: pathlib.Path) -> None:
  """A missing metadata file names the path it looked for."""
  with pytest.raises(genre.GenreError, match="not found"):
    genre.load_classes(tmp_path / "absent.json")


@pytest.mark.parametrize("document", [{}, {"classes": []}, {"classes": "no"}])
def test_load_classes_rejects_metadata_without_classes(
    tmp_path: pathlib.Path, document: object) -> None:
  """Metadata carrying no usable class list is an error."""
  path = write_metadata(tmp_path, document)
  with pytest.raises(genre.GenreError, match="lists no classes"):
    genre.load_classes(path)


def test_load_classes_rejects_invalid_json(tmp_path: pathlib.Path) -> None:
  """A corrupt metadata file is reported as such."""
  path = tmp_path / genre.CLASSIFIER_METADATA_FILE
  path.write_text("{not json", encoding="utf-8")
  with pytest.raises(genre.GenreError, match="not valid JSON"):
    genre.load_classes(path)


def test_missing_models_lists_everything_when_absent(
    tmp_path: pathlib.Path) -> None:
  """An empty directory is missing every model file."""
  assert set(genre.missing_models(tmp_path)) == set(genre.MODEL_URLS)


def test_missing_models_is_empty_once_present(tmp_path: pathlib.Path) -> None:
  """Nothing is missing once all three files exist."""
  for name in genre.MODEL_URLS:
    (tmp_path / name).write_bytes(b"x")
  assert not genre.missing_models(tmp_path)


def test_detector_reports_missing_models(tmp_path: pathlib.Path) -> None:
  """Building a detector without models says how to get them."""
  with pytest.raises(genre.GenreError, match="fetch-models"):
    genre.GenreDetector(tmp_path)


def test_detector_reports_missing_essentia(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """With models but no Essentia, the error says what to install."""
  for name in genre.MODEL_URLS:
    (tmp_path / name).write_bytes(b"x")
  monkeypatch.setattr(genre, "have_essentia", lambda: False)
  with pytest.raises(genre.GenreError, match="essentia-tensorflow"):
    genre.GenreDetector(tmp_path)


def test_model_urls_cover_every_required_file() -> None:
  """Every file the detector needs is one fetch-models can download."""
  required = {
      genre.EMBEDDING_MODEL_FILE,
      genre.CLASSIFIER_MODEL_FILE,
      genre.CLASSIFIER_METADATA_FILE,
  }
  assert required == set(genre.MODEL_URLS)
  assert all(url.startswith("https://") for url in genre.MODEL_URLS.values())
