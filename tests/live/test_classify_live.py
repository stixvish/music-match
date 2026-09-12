"""The essentia model still loads and classifies (SPEC.md §19)."""

import subprocess

import pytest

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def long_audio(tmp_path_factory):
  """Thirty seconds of tone.

  The shared two-second fixture is too short: the model's embedding window
  needs real duration and fails with `LIST_EMPTY to MATRIX_REAL` on a clip
  that brief, which looks like a broken model rather than a short input.
  """
  path = tmp_path_factory.mktemp("long") / "tone.wav"
  subprocess.run(
    [
      "ffmpeg",
      "-y",
      "-v",
      "error",
      "-f",
      "lavfi",
      "-i",
      "sine=frequency=440:duration=30",
      "-ar",
      "44100",
      "-ac",
      "2",
      str(path),
    ],
    check=True,
  )
  return path


def test_the_classifier_model_loads_and_predicts(long_audio):
  """Routing depends on this; the model is downloaded, not vendored.

  Scoped to routing only — its genre output is not trusted as a tag, having
  scored 0 of 49 on real Bollywood material (SPEC.md §7).
  """
  pytest.importorskip("essentia", reason="classify group not installed")
  from music.classify import Classifier

  predictions = Classifier().predict(long_audio, top=1)
  assert predictions, "the classifier returned nothing"
  assert predictions[0].genre, "no top-level genre, which is what routes"
