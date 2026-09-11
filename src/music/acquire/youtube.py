"""Fetch audio from YouTube via the yt-dlp Python API.

The library is used rather than the CLI so the structured info dict is
available directly: `channel` and `description` drive art-track detection, and
`abr` drives the quality gate (SPEC.md §6).
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from music.config import YouTubeConfig

log = logging.getLogger(__name__)

# a label-provided "art track" carries clean album audio; a regular upload may
# be a music video with an intro skit (SPEC.md §8).
ART_TRACK_MARKER = "Provided to YouTube by"


class QualityError(RuntimeError):
  """Raised when a download lands below the configured bitrate floor."""


@dataclass(frozen=True)
class VideoRef:
  """One entry from a playlist listing."""

  video_id: str
  title: str
  channel: str
  duration_s: float | None


@dataclass(frozen=True)
class Download:
  """A file on disk plus the metadata yt-dlp reported."""

  video_id: str
  path: Path
  title: str
  channel: str
  description: str
  duration_s: float
  abr: float
  itag: str

  @property
  def is_art_track(self) -> bool:
    """Whether this looks like label-provided album audio."""
    return self.channel.endswith(" - Topic") or (ART_TRACK_MARKER in self.description)


def _base_opts(cfg: YouTubeConfig) -> dict[str, object]:
  return {
    "format": cfg.format_chain,
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    # itag 141 is served only to the web_music client with premium cookies.
    "extractor_args": {"youtube": {"player_client": [cfg.player_client]}},
    "cookiesfrombrowser": (cfg.cookie_browser,),
  }


def enumerate_playlist(url: str, cfg: YouTubeConfig) -> list[VideoRef]:
  """List a playlist without downloading anything.

  Args:
    url: Playlist URL.
    cfg: YouTube settings.

  Returns:
    One VideoRef per entry, skipping unavailable items.
  """
  opts = _base_opts(cfg) | {"extract_flat": "in_playlist", "skip_download": True}
  with yt_dlp.YoutubeDL(opts) as ydl:
    info = ydl.extract_info(url, download=False)
  entries = (info or {}).get("entries") or []
  refs = []
  for entry in entries:
    if not entry or not entry.get("id"):
      continue
    refs.append(
      VideoRef(
        video_id=str(entry["id"]),
        title=str(entry.get("title") or ""),
        channel=str(entry.get("channel") or entry.get("uploader") or ""),
        duration_s=entry.get("duration"),
      )
    )
  return refs


def download(video_id: str, dest: Path, cfg: YouTubeConfig) -> Download:
  """Fetch one track's audio.

  Args:
    video_id: YouTube video id.
    dest: Directory to write into.
    cfg: YouTube settings.

  Returns:
    A Download describing the result.

  Raises:
    QualityError: If the best available stream is below the bitrate floor.
    RuntimeError: If yt-dlp returns no usable info.
  """
  dest.mkdir(parents=True, exist_ok=True)
  opts = _base_opts(cfg) | {"outtmpl": str(dest / "%(id)s.%(ext)s")}
  url = f"https://www.youtube.com/watch?v={video_id}"

  with yt_dlp.YoutubeDL(opts) as ydl:
    info = ydl.extract_info(url, download=True)
  if not info:
    raise RuntimeError(f"yt-dlp returned nothing for {video_id}")

  abr = float(info.get("abr") or 0.0)
  # fail loudly rather than silently accept a downgrade (SPEC.md §6).
  if abr and abr < cfg.min_bitrate_kbps:
    raise QualityError(
      f"{video_id}: best available {abr:.0f} kbps is below the "
      f"{cfg.min_bitrate_kbps} kbps floor — check cookies are authenticated"
    )

  path = Path(ydl.prepare_filename(info))
  if not path.exists():
    matches = sorted(dest.glob(f"{video_id}.*"))
    if not matches:
      raise RuntimeError(f"downloaded file for {video_id} not found in {dest}")
    path = matches[0]

  return Download(
    video_id=str(info.get("id") or video_id),
    path=path,
    title=str(info.get("title") or ""),
    channel=str(info.get("channel") or info.get("uploader") or ""),
    description=str(info.get("description") or ""),
    duration_s=float(info.get("duration") or 0.0),
    abr=abr,
    itag=str(info.get("format_id") or ""),
  )
