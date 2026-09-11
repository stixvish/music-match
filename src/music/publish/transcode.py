"""Decode to AIFF.

AIFF, not FLAC: rekordbox silently drops album artist, release date, original
artist, mix name and lyricist from FLAC, two of which are required fields
(SPEC.md §10).
"""

import subprocess
from pathlib import Path


def to_aiff(source: Path, dest: Path) -> Path:
  """Transcode any supported input to 16-bit AIFF.

  `-sample_fmt s16` is mandatory: ffmpeg defaults to 24-bit when decoding AAC,
  which inflates output by ~40% for no benefit — the source is lossy (SPEC.md
  §4). The sample rate is left alone; resampling a 44.1 kHz master gains
  nothing and CDJs play both.

  Args:
    source: Input audio file.
    dest: Output path. Parent directories are created.

  Returns:
    The destination path.

  Raises:
    RuntimeError: If ffmpeg fails.
  """
  dest.parent.mkdir(parents=True, exist_ok=True)
  result = subprocess.run(
    [
      "ffmpeg",
      "-y",
      "-v",
      "error",
      "-i",
      str(source),
      "-vn",
      "-map_metadata",
      "-1",
      "-c:a",
      "pcm_s16be",
      "-sample_fmt",
      "s16",
      str(dest),
    ],
    capture_output=True,
    text=True,
    check=False,
  )
  if result.returncode != 0:
    raise RuntimeError(f"ffmpeg failed on {source.name}: {result.stderr[:300]}")
  return dest


def extract_artwork(source: Path, dest: Path) -> Path | None:
  """Pull embedded cover art out of a file, if it has any.

  Args:
    source: Input audio file.
    dest: Where to write the image.

  Returns:
    The image path, or None if the file carries no artwork.
  """
  dest.parent.mkdir(parents=True, exist_ok=True)
  result = subprocess.run(
    [
      "ffmpeg",
      "-y",
      "-v",
      "quiet",
      "-i",
      str(source),
      "-an",
      "-vcodec",
      "copy",
      str(dest),
    ],
    capture_output=True,
    check=False,
  )
  return dest if result.returncode == 0 and dest.exists() else None


def to_preview(source: Path, dest: Path) -> Path | None:
  """Encode a small AAC preview so a reviewer can hear a published file.

  No browser decodes AIFF — Chrome reports an empty `canPlayType` for both
  `audio/x-aiff` and `audio/aiff` (SPEC.md §15) — so a published track has to
  be re-encoded before it can be auditioned. Only reached once the download
  source has been cleared; while staging survives, that file is served
  directly and nothing is encoded.

  Args:
    source: A published AIFF.
    dest: Where to write the preview.

  Returns:
    The preview path, or None if ffmpeg fails. Losing a preview is not worth
    failing the request over.
  """
  dest.parent.mkdir(parents=True, exist_ok=True)
  result = subprocess.run(
    [
      "ffmpeg",
      "-y",
      "-v",
      "error",
      "-i",
      str(source),
      "-vn",
      "-c:a",
      "aac",
      "-b:a",
      "192k",
      str(dest),
    ],
    capture_output=True,
    check=False,
  )
  return dest if result.returncode == 0 and dest.exists() else None
