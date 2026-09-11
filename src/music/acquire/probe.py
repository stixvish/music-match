"""Read technical properties out of an audio file with ffprobe."""

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AudioProbe:
  """What ffprobe can tell us about a file."""

  duration_s: float
  codec: str
  bitrate: int
  sample_rate: int
  channels: int
  tags: dict[str, str]

  @property
  def bitrate_kbps(self) -> int:
    """Audio-stream bitrate in kbps."""
    return self.bitrate // 1000


def sha256(path: Path) -> str:
  """Hash a file's contents.

  Args:
    path: File to hash.

  Returns:
    Lowercase hex digest.
  """
  digest = hashlib.sha256()
  with path.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1 << 20), b""):
      digest.update(chunk)
  return digest.hexdigest()


def probe(path: Path) -> AudioProbe:
  """Inspect an audio file.

  Uses the audio *stream* bitrate, not the container's: the container figure
  includes embedded cover art and overstates quality by ~6% (SPEC.md §4).

  Args:
    path: Audio file.

  Returns:
    An AudioProbe.

  Raises:
    RuntimeError: If ffprobe fails or reports no audio stream.
  """
  result = subprocess.run(
    [
      "ffprobe",
      "-v",
      "quiet",
      "-print_format",
      "json",
      "-show_format",
      "-show_streams",
      str(path),
    ],
    capture_output=True,
    text=True,
    check=False,
  )
  if result.returncode != 0:
    raise RuntimeError(f"ffprobe failed on {path.name}: {result.stderr[:200]}")

  data = json.loads(result.stdout)
  streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
  if not streams:
    raise RuntimeError(f"no audio stream in {path.name}")
  stream = streams[0]
  fmt = data.get("format", {})

  skip = {"major_brand", "minor_version", "compatible_brands", "encoder"}
  tags = {k.lower(): v for k, v in fmt.get("tags", {}).items() if k.lower() not in skip}
  return AudioProbe(
    duration_s=float(fmt.get("duration", 0.0)),
    codec=str(stream.get("codec_name", "")),
    bitrate=int(stream.get("bit_rate") or 0),
    sample_rate=int(stream.get("sample_rate") or 0),
    channels=int(stream.get("channels") or 0),
    tags=tags,
  )
