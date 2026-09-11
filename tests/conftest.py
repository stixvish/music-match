"""Shared fixtures."""

import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def synth_audio(tmp_path_factory) -> Path:
  """A tiny real AAC file, synthesized by ffmpeg.

  Generating audio means no binary fixtures are committed (SPEC.md §19).
  """
  path = tmp_path_factory.mktemp("audio") / "sine.m4a"
  subprocess.run(
    [
      "ffmpeg",
      "-y",
      "-v",
      "error",
      "-f",
      "lavfi",
      "-i",
      "sine=frequency=440:duration=2",
      "-ar",
      "44100",
      "-ac",
      "2",
      "-c:a",
      "aac",
      "-b:a",
      "256k",
      str(path),
    ],
    check=True,
  )
  return path
