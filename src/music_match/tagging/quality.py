"""Ranking two copies of the same audio by quality.

When dedup layer 3 finds a genuine duplicate, the higher-quality file is
the one to keep. The ordering ARCHITECTURE specifies is format tier
first — lossless beats lossy, whatever the bitrates say — then bitrate
as the tiebreak.
"""

import dataclasses
import pathlib

import mutagen
from mutagen import aiff
from mutagen import flac
from mutagen import mp4
from mutagen import wave

# MP4 is the one container here that may hold either lossy or lossless
# audio, so it is classified by codec rather than by container.
_LOSSLESS_TYPES = (flac.FLAC, wave.WAVE, aiff.AIFF)
_LOSSLESS_MP4_CODECS = ("alac",)


class QualityError(Exception):
  """Raised when a file's audio properties cannot be read."""


@dataclasses.dataclass(frozen=True)
class AudioQuality:
  """What is known about one file's audio quality.

  Attributes:
    lossless: Whether the audio is stored without loss.
    bitrate: Bits per second, 0 if the format does not report it.
    sample_rate: Samples per second, 0 if unknown.
    bits_per_sample: Bit depth, 0 if unknown.
    codec: A short human-readable codec or container name.
  """
  lossless: bool
  bitrate: int
  sample_rate: int
  bits_per_sample: int
  codec: str

  def rank(self) -> tuple[int, int, int, int]:
    """Returns a sort key, higher being better.

    Format tier dominates, so a 320kbps MP3 never outranks a lossless
    file. Bitrate is the tiebreak within a tier, as ARCHITECTURE
    specifies, with sample rate and bit depth below it to keep the
    ordering total rather than leaving equal-bitrate files unordered.

    Returns:
      The comparison key.
    """
    return (int(self.lossless), self.bitrate, self.sample_rate,
            self.bits_per_sample)

  def describe(self) -> str:
    """Returns a one-line summary for CLI output."""
    tier = "lossless" if self.lossless else "lossy"
    return f"{self.codec} {tier} {self.bitrate // 1000}kbps"


def probe(path: pathlib.Path) -> AudioQuality:
  """Reads a file's audio properties.

  Args:
    path: The audio file to inspect.

  Returns:
    The file's quality.

  Raises:
    QualityError: If the file cannot be read or is not audio mutagen
      recognizes.
  """
  try:
    audio = mutagen.File(path)
  except (OSError, mutagen.MutagenError) as err:
    raise QualityError(f"could not read {path}: {err}") from err
  if audio is None or audio.info is None:
    raise QualityError(f"unrecognized audio format: {path}")

  info = audio.info
  codec = str(getattr(info, "codec", "") or type(audio).__name__).lower()
  return AudioQuality(
      lossless=_is_lossless(audio, codec),
      bitrate=int(getattr(info, "bitrate", 0) or 0),
      sample_rate=int(getattr(info, "sample_rate", 0) or 0),
      bits_per_sample=int(getattr(info, "bits_per_sample", 0) or 0),
      codec=codec,
  )


def _is_lossless(audio: mutagen.FileType, codec: str) -> bool:
  """Decides whether a file's audio is lossless.

  Args:
    audio: The loaded file.
    codec: Its lowercased codec or container name.

  Returns:
    True if the audio is stored without loss.
  """
  if isinstance(audio, _LOSSLESS_TYPES):
    return True
  if isinstance(audio, mp4.MP4):
    return codec.startswith(_LOSSLESS_MP4_CODECS)
  return False
