"""Local genre detection with Essentia's discogs-effnet model.

Runs entirely offline. Two uses, per ARCHITECTURE: the detected genre
selects which source-precedence list to query, and it becomes the
fallback genre tag when no external source returns one. It is also a
cross-check — Essentia saying House while the matched release says Drum
& Bass is evidence the *match* is wrong, not just the genre.

The model emits 400 labels in Discogs' own `Genre---Style` vocabulary
("Electronic---Deep House"), which is the whole reason for choosing it:
local detection and Discogs speak the same language, and
`config.normalize_genre` reduces a label to a precedence.toml key
without the config having to enumerate styles.

Essentia is an optional install — see SETUP.md — so it is imported
lazily, inside the detector rather than at module scope. Everything
above that boundary is pure Python and testable without it, which is
what keeps the unit suite hermetic and CI free of a 94MB wheel.
"""

import contextlib
import dataclasses
import json
import os
import pathlib
from typing import Any, Iterator, Iterable, Sequence

DEFAULT_MODELS_DIR = pathlib.Path("models")

EMBEDDING_MODEL_FILE = "discogs-effnet-bs64-1.pb"
CLASSIFIER_MODEL_FILE = "genre_discogs400-discogs-effnet-1.pb"
CLASSIFIER_METADATA_FILE = "genre_discogs400-discogs-effnet-1.json"

# Fixed by the model: it was trained on 16kHz mono audio, and these are
# the graph nodes its published metadata names.
MODEL_SAMPLE_RATE = 16000
EMBEDDING_OUTPUT_NODE = "PartitionedCall:1"
CLASSIFIER_INPUT_NODE = "serving_default_model_Placeholder"
CLASSIFIER_OUTPUT_NODE = "PartitionedCall:0"

DEFAULT_TOP_N = 5

# Set to anything non-empty to let Essentia's own logging through. Off by
# default because the TensorFlow predict algorithms emit
# "No network created, or last created network has been deleted..." many
# times *per file* — thousands of lines over a library-sized run, none of
# it actionable. Real failures reach us as exceptions, not log lines.
ESSENTIA_LOGS_VAR = "MUSIC_MATCH_ESSENTIA_LOGS"

# Below this the model is guessing. Measured against tracks with known
# genres: the top-level genre is right about 22% of the time under 0.15
# confidence, 87% between 0.25 and 0.40, and 91% above that. Callers that
# act on a detected genre should check `GenreResult.is_confident` rather
# than trusting a bare label.
DEFAULT_CONFIDENCE_FLOOR = 0.15

MODEL_BASE_URL = "https://essentia.upf.edu/models"
MODEL_URLS = {
    EMBEDDING_MODEL_FILE: f"{MODEL_BASE_URL}/feature-extractors/discogs-effnet/"
                          f"{EMBEDDING_MODEL_FILE}",
    CLASSIFIER_MODEL_FILE:
        f"{MODEL_BASE_URL}/classification-heads/genre_discogs400/"
        f"{CLASSIFIER_MODEL_FILE}",
    CLASSIFIER_METADATA_FILE:
        f"{MODEL_BASE_URL}/classification-heads/genre_discogs400/"
        f"{CLASSIFIER_METADATA_FILE}",
}


class GenreError(Exception):
  """Raised when genre detection cannot run or a file cannot be analysed."""


@dataclasses.dataclass(frozen=True)
class Prediction:
  """One predicted label and how strongly the model backed it.

  Attributes:
    label: The full Discogs label, e.g. "Electronic---Deep House".
    confidence: The model's score for it, between 0 and 1.
  """
  label: str
  confidence: float

  @property
  def genre(self) -> str:
    """Returns the top-level genre, e.g. "Electronic"."""
    return self.label.split("---")[0]

  @property
  def style(self) -> str | None:
    """Returns the style below the genre, e.g. "Deep House", if any."""
    _, separator, style = self.label.partition("---")
    return style if separator else None


@dataclasses.dataclass(frozen=True)
class GenreResult:
  """What the model made of one file.

  Attributes:
    predictions: The highest-scoring labels, strongest first.
  """
  predictions: tuple[Prediction, ...]

  @property
  def top(self) -> Prediction | None:
    """Returns the strongest prediction, or None if there were none."""
    return self.predictions[0] if self.predictions else None

  @property
  def label(self) -> str | None:
    """Returns the strongest label, or None if there were none."""
    return self.top.label if self.top else None

  def is_confident(self, minimum: float) -> bool:
    """Returns whether the top prediction clears a confidence floor.

    Args:
      minimum: The score the top prediction must reach.

    Returns:
      True if there is a prediction and it scores at least `minimum`.
    """
    return self.top is not None and self.top.confidence >= minimum


def mean_over_frames(frames: Iterable[Sequence[float]]) -> list[float]:
  """Averages per-frame predictions into one score per label.

  The model scores every ~2 seconds of audio separately. Averaging is
  what turns "this passage sounds like Trap" into a judgement about the
  track as a whole, and stops one atypical intro deciding the answer.

  Args:
    frames: One sequence of per-label scores per analysed frame.

  Returns:
    The mean score for each label, or an empty list if there were no
    frames.

  Raises:
    GenreError: If the frames are not all the same length.
  """
  totals: list[float] = []
  count = 0
  for frame in frames:
    if not totals:
      totals = [0.0] * len(frame)
    elif len(frame) != len(totals):
      raise GenreError(
          f"model returned frames of differing width: {len(frame)} != "
          f"{len(totals)}")
    for index, value in enumerate(frame):
      totals[index] += value
    count += 1
  if not count:
    return []
  return [total / count for total in totals]


def rank_predictions(scores: Sequence[float],
                     classes: Sequence[str],
                     limit: int = DEFAULT_TOP_N) -> tuple[Prediction, ...]:
  """Turns per-label scores into the strongest predictions.

  Args:
    scores: One score per label, in the model's class order.
    classes: The label names, in the same order.
    limit: How many predictions to keep.

  Returns:
    The highest-scoring predictions, strongest first. Ties break on label
    so the ordering is stable.

  Raises:
    GenreError: If there is not exactly one score per label.
  """
  if len(scores) != len(classes):
    raise GenreError(f"model returned {len(scores)} scores for "
                     f"{len(classes)} labels")
  ranked = sorted((Prediction(label=label, confidence=float(score))
                   for label, score in zip(classes, scores)),
                  key=lambda item: (-item.confidence, item.label))
  return tuple(ranked[:limit])


def result_from_frames(frames: Iterable[Sequence[float]],
                       classes: Sequence[str],
                       top_n: int = DEFAULT_TOP_N) -> GenreResult:
  """Turns raw per-frame model output into a ranked result.

  Split out from the detector so the whole path from model output to
  answer is testable without Essentia installed.

  Args:
    frames: One sequence of per-label scores per analysed frame.
    classes: The label names, in the model's output order.
    top_n: How many predictions to keep.

  Returns:
    The ranked predictions, empty if there were no frames to average.

  Raises:
    GenreError: If the frames do not line up with the label list.
  """
  scores = mean_over_frames(frames)
  if not scores:
    return GenreResult(predictions=())
  return GenreResult(predictions=rank_predictions(scores, classes, top_n))


def load_classes(metadata_path: pathlib.Path) -> tuple[str, ...]:
  """Reads the label list from the classifier's metadata file.

  Args:
    metadata_path: Path to the model's JSON metadata.

  Returns:
    The label names, in the order the model outputs them.

  Raises:
    GenreError: If the file is missing or does not carry a class list.
  """
  try:
    with metadata_path.open("rb") as handle:
      document: dict[str, Any] = json.load(handle)
  except FileNotFoundError as err:
    raise GenreError(f"model metadata not found: {metadata_path}") from err
  except json.JSONDecodeError as err:
    raise GenreError(f"{metadata_path} is not valid JSON: {err}") from err

  classes = document.get("classes")
  if not isinstance(classes, list) or not classes:
    raise GenreError(f"{metadata_path} lists no classes")
  return tuple(str(item) for item in classes)


def missing_models(
    models_dir: pathlib.Path = DEFAULT_MODELS_DIR) -> tuple[str, ...]:
  """Returns the names of model files that are not present.

  Args:
    models_dir: Where the model files should be.

  Returns:
    The missing file names, empty if everything is in place.
  """
  return tuple(name for name in MODEL_URLS if not (models_dir / name).is_file())


def essentia_logs_enabled() -> bool:
  """Returns whether Essentia's own log output should be left alone.

  Returns:
    True if the escape-hatch environment variable is set.
  """
  return bool(os.environ.get(ESSENTIA_LOGS_VAR, "").strip())


def _import_essentia_standard() -> Any:
  """Imports `essentia.standard`, quieting TensorFlow first.

  TensorFlow's C++ logging is configured from the environment when it is
  first loaded, so this has to happen before the import rather than after.

  Returns:
    The `essentia.standard` module.

  Raises:
    ImportError: If Essentia is not installed.
  """
  if not essentia_logs_enabled():
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
  from essentia import standard  # pylint: disable=import-outside-toplevel
  return standard


@contextlib.contextmanager
def quiet_essentia(*,
                   warnings: bool = True,
                   info: bool = True) -> Iterator[None]:
  """Silences Essentia's log streams for the duration of a block.

  Scoped rather than global, and restores whatever was set before, so a
  caller that wants the logs can have them.

  Args:
    warnings: Whether to silence the warning stream.
    info: Whether to silence the info stream.

  Yields:
    None.
  """
  if essentia_logs_enabled():
    yield
    return
  import essentia  # pylint: disable=import-outside-toplevel
  previous = (essentia.log.warningActive, essentia.log.infoActive)
  if warnings:
    essentia.log.warningActive = False
  if info:
    essentia.log.infoActive = False
  try:
    yield
  finally:
    essentia.log.warningActive, essentia.log.infoActive = previous


def have_essentia() -> bool:
  """Returns whether Essentia can be imported.

  Note that the algorithms live in `essentia.standard`; there is no
  `essentia.tensorflow` module in any build, so importing that is not a
  valid check.

  Returns:
    True if `essentia.standard` imports and carries the TensorFlow
    algorithms this module needs.
  """
  try:
    standard = _import_essentia_standard()
  except ImportError:
    return False
  return hasattr(standard, "TensorflowPredictEffnetDiscogs")


class GenreDetector:
  """Runs the discogs-effnet model over audio files.

  The models are loaded once and reused, which matters: the embedding
  graph is 18MB and reloading it per file would dominate the runtime of
  a pass over a few thousand tracks.
  """

  def __init__(self,
               models_dir: pathlib.Path = DEFAULT_MODELS_DIR,
               *,
               top_n: int = DEFAULT_TOP_N) -> None:
    """Loads the models.

    Args:
      models_dir: Where the model files live.
      top_n: How many predictions to keep per file.

    Raises:
      GenreError: If Essentia is not installed or the model files are
        missing.
    """
    self._models_dir = models_dir
    self._top_n = top_n
    missing = missing_models(models_dir)
    if missing:
      names = ", ".join(missing)
      raise GenreError(f"missing model files in {models_dir}: {names}."
                       " Run `music-match genre fetch-models`.")
    if not have_essentia():
      raise GenreError(
          "essentia is not installed. Run `uv pip install essentia-tensorflow`"
          " (see SETUP.md).")
    self._classes = load_classes(models_dir / CLASSIFIER_METADATA_FILE)
    self._embedder, self._classifier = self._build_algorithms()

  def _build_algorithms(self) -> tuple[Any, Any]:
    """Constructs the two Essentia algorithms this detector chains.

    Returns:
      The embedding model and the classifier head.
    """
    # Imported here rather than at module scope: Essentia is an optional
    # install and everything above this class works without it.
    standard = _import_essentia_standard()
    with quiet_essentia():
      embedder = standard.TensorflowPredictEffnetDiscogs(
          graphFilename=str(self._models_dir / EMBEDDING_MODEL_FILE),
          output=EMBEDDING_OUTPUT_NODE)
      classifier = standard.TensorflowPredict2D(graphFilename=str(
          self._models_dir / CLASSIFIER_MODEL_FILE),
                                                input=CLASSIFIER_INPUT_NODE,
                                                output=CLASSIFIER_OUTPUT_NODE)
    return embedder, classifier

  @property
  def classes(self) -> tuple[str, ...]:
    """Returns the labels this detector can predict."""
    return self._classes

  def detect(self, path: pathlib.Path) -> GenreResult:
    """Detects the genre of one audio file.

    Args:
      path: The audio file to analyse.

    Returns:
      The strongest predictions for it. Empty if the file held too little
      audio for the model to score a single frame.

    Raises:
      GenreError: If the file cannot be decoded or the model fails on it.
    """
    return result_from_frames(self._predict(path), self._classes, self._top_n)

  def _predict(self, path: pathlib.Path) -> list[list[float]]:
    """Runs the model chain over a file.

    Args:
      path: The audio file to analyse.

    Returns:
      Per-frame scores, one inner list per analysed frame.

    Raises:
      GenreError: If the file cannot be decoded or the model fails.
    """
    standard = _import_essentia_standard()
    try:
      # The loader's warnings are left on: a decode problem is worth
      # hearing about. Only the predict algorithms are silenced.
      audio = standard.MonoLoader(filename=str(path),
                                  sampleRate=MODEL_SAMPLE_RATE,
                                  resampleQuality=4)()
      with quiet_essentia():
        embeddings = self._embedder(audio)
        # A file shorter than one analysis window embeds to nothing, and
        # handing that to the classifier raises out of the binding layer
        # rather than returning empty. Short files are ordinary in a real
        # library — intros, skits, jingles — and must not end the run.
        if len(embeddings) == 0:
          return []
        predictions = self._classifier(embeddings)
    except (RuntimeError, TypeError, ValueError) as err:
      # Essentia surfaces decode and shape problems through its binding
      # layer as several exception types, not just RuntimeError.
      raise GenreError(f"could not analyse {path}: {err}") from err
    return [list(frame) for frame in predictions]
