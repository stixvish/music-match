"""Tests for audio quality ranking."""

import pathlib

import pytest

from music_match.tagging import quality


def quality_of(*,
               lossless: bool,
               bitrate: int,
               sample_rate: int = 44100,
               bits: int = 16) -> quality.AudioQuality:
  """Builds an AudioQuality without touching a file.

  Args:
    lossless: Whether the audio is lossless.
    bitrate: Bits per second.
    sample_rate: Samples per second.
    bits: Bit depth.

  Returns:
    The constructed quality.
  """
  return quality.AudioQuality(lossless=lossless,
                              bitrate=bitrate,
                              sample_rate=sample_rate,
                              bits_per_sample=bits,
                              codec="test")


def test_lossless_beats_a_higher_bitrate_lossy_file() -> None:
  """Format tier dominates: no lossy bitrate outranks lossless."""
  lossy = quality_of(lossless=False, bitrate=320_000)
  lossless = quality_of(lossless=True, bitrate=141_000)
  assert lossless.rank() > lossy.rank()


def test_bitrate_breaks_ties_within_a_tier() -> None:
  """Among lossy files, the higher bitrate wins."""
  assert (quality_of(lossless=False, bitrate=320_000).rank()
          > quality_of(lossless=False, bitrate=128_000).rank())


def test_sample_rate_breaks_equal_bitrates() -> None:
  """Equal bitrates fall through to sample rate, keeping order total."""
  assert (quality_of(lossless=True, bitrate=141_000, sample_rate=48_000).rank()
          > quality_of(lossless=True, bitrate=141_000,
                       sample_rate=44_100).rank())


def test_describe_is_readable() -> None:
  """The CLI summary names the tier and bitrate."""
  described = quality_of(lossless=False, bitrate=256_000).describe()
  assert "lossy" in described
  assert "256kbps" in described


def test_probe_reads_a_lossy_file(m4a_file: pathlib.Path) -> None:
  """An AAC file in an MP4 container is classified lossy."""
  probed = quality.probe(m4a_file)
  assert probed.lossless is False
  assert probed.sample_rate > 0


def test_probe_reads_a_lossless_file(wav_file: pathlib.Path) -> None:
  """A WAV file is classified lossless with a real bitrate."""
  probed = quality.probe(wav_file)
  assert probed.lossless is True
  assert probed.bitrate > 0
  assert probed.bits_per_sample == 16


def test_probed_lossless_outranks_probed_lossy(wav_file: pathlib.Path,
                                               m4a_file: pathlib.Path) -> None:
  """The ranking holds on real files, not just constructed ones."""
  assert quality.probe(wav_file).rank() > quality.probe(m4a_file).rank()


def test_probe_rejects_a_non_audio_file(tmp_path: pathlib.Path) -> None:
  """A file mutagen cannot identify raises QualityError."""
  path = tmp_path / "notes.txt"
  path.write_text("not audio", encoding="utf-8")
  with pytest.raises(quality.QualityError, match="unrecognized"):
    quality.probe(path)


def test_probe_rejects_a_missing_file(tmp_path: pathlib.Path) -> None:
  """A missing file names the path it could not read."""
  with pytest.raises(quality.QualityError):
    quality.probe(tmp_path / "gone.m4a")
