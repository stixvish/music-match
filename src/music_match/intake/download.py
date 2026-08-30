"""Downloading audio, and stamping it so it can be recognised later.

Every downloaded file gets its source id written into a non-Rekordbox tag
field. That is what lets `reindex` rebuild the download archive from the
files alone, so losing the database costs a rescan rather than
re-downloading two thousand tracks.

Format selection asks for an audio-only m4a first. The library is already
M4A, and taking a stream that is already in that container means no
transcode, no quality loss, and no dependency on ffmpeg being installed.
"""

import dataclasses
import pathlib
from typing import Any

import yt_dlp

from music_match.intake.entries import Entry
from music_match.intake.entries import IntakeError
from music_match.tagging import tags as tag_io
from music_match.tagging.fields import TrackTags

# Prefer an audio-only m4a; fall back to any audio-only stream, then to
# whatever exists. Only the first avoids a transcode.
FORMAT_SELECTOR = "bestaudio[ext=m4a]/bestaudio/best"

# yt-dlp writes "Uploader - Title.ext", which is the convention the rest
# of this library already follows and the one the matcher falls back to
# when a file carries no tags.
OUTPUT_TEMPLATE = "%(uploader)s - %(title)s.%(ext)s"


@dataclasses.dataclass(frozen=True)
class Download:
  """A completed download.

  Attributes:
    entry: What was downloaded.
    path: Where it landed.
    stamped: Whether the source id was written into the file.
  """
  entry: Entry
  path: pathlib.Path
  stamped: bool


def options_for(destination: pathlib.Path,
                extra: dict[str, Any] | None = None) -> dict[str, Any]:
  """Builds the yt-dlp options used for a real download.

  Args:
    destination: The folder to download into.
    extra: Options to override the defaults.

  Returns:
    The option dictionary.
  """
  return {
      "quiet": True,
      "no_warnings": True,
      # `quiet` does not cover the progress bar, which redraws several
      # times a second and buries the per-track output over a batch.
      "noprogress": True,
      "format": FORMAT_SELECTOR,
      "outtmpl": str(destination / OUTPUT_TEMPLATE),
      "noplaylist": True,
      "restrictfilenames": False,
      **(extra or {}),
  }


def downloaded_path(info: dict[str, Any]) -> pathlib.Path | None:
  """Finds where yt-dlp actually wrote a file.

  Args:
    info: The info dict returned after a download.

  Returns:
    The path, or None if yt-dlp reported none.
  """
  downloads = info.get("requested_downloads")
  if isinstance(downloads, list) and downloads:
    first = downloads[0]
    if isinstance(first, dict):
      target = first.get("filepath") or first.get("_filename")
      if target:
        return pathlib.Path(str(target))
  target = info.get("filepath") or info.get("_filename")
  return pathlib.Path(str(target)) if target else None


def stamp_source_id(path: pathlib.Path, entry: Entry) -> bool:
  """Writes the source id into the file's internal bookkeeping field.

  Args:
    path: The downloaded file.
    entry: The entry it came from.

  Returns:
    True if the id was written. A format whose tags cannot be read is
    reported rather than raised: the download itself succeeded, and the
    archive row in the database still records it.
  """
  try:
    tag_io.write_tags(path, TrackTags(source_video_id=entry.video_id))
  except tag_io.TagError:
    return False
  return True


def download_entry(entry: Entry,
                   destination: pathlib.Path,
                   *,
                   options: dict[str, Any] | None = None) -> Download:
  """Downloads one entry's audio and stamps it.

  Args:
    entry: What to download.
    destination: The folder to download into.
    options: yt-dlp options to override the defaults.

  Returns:
    The completed download.

  Raises:
    IntakeError: If the download fails or produces no file.
  """
  destination.mkdir(parents=True, exist_ok=True)
  settings = options_for(destination, options)
  try:
    with yt_dlp.YoutubeDL(settings) as downloader:
      info = downloader.extract_info(entry.url or entry.video_id, download=True)
  except yt_dlp.utils.DownloadError as err:
    raise IntakeError(f"could not download {entry.label()}: {err}") from err
  if not isinstance(info, dict):
    raise IntakeError(f"no information returned for {entry.label()}")

  path = downloaded_path(info)
  if path is None or not path.exists():
    raise IntakeError(f"{entry.label()} reported no downloaded file")
  return Download(entry=entry, path=path, stamped=stamp_source_id(path, entry))
