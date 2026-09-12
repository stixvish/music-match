"""Shared fixtures."""

import subprocess
from pathlib import Path

import pytest

from music import config


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path_factory, monkeypatch) -> Path:
  """Point library, staging and database at a temp directory.

  `config.load()` defaults to the real `~/Music/library`, so any test that
  publishes writes into the user's actual library. Two stray fixture tracks
  were found there before this existed — the synthesized sine, filed under
  "An Artist", ready to be imported into rekordbox alongside real music.
  """
  root = tmp_path_factory.mktemp("home")
  toml = root / "config.toml"
  toml.write_text(
    "[paths]\n"
    f'library = "{root / "library"}"\n'
    f'staging = "{root / "staging"}"\n'
    f'database = "{root / "music.db"}"\n'
  )
  monkeypatch.setattr(config, "DEFAULT_CONFIG", toml)
  return root


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
