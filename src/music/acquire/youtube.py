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
  """Build yt-dlp options with full CLI parity.

  Options are derived from yt-dlp's own argument parser rather than assembled
  by hand, so CLI defaults such as `js_runtimes` are inherited.

  Two things are required for the premium formats (itag 141/774) to appear at
  all, and both fail silently:

  1. `deno` on PATH, plus the `yt-dlp-ejs` package, which together solve
     YouTube's javascript "n" challenge. The brew CLI bundles `yt-dlp-ejs`;
     a plain `pip install yt-dlp` does not. Without it yt-dlp reports
     "Only images are available for download".
  2. Authenticated premium cookies, for the web_music client.

  Symptom of either: "Requested format is not available" from the library on
  a video the CLI downloads fine.

  Args:
    cfg: YouTube settings.

  Returns:
    An options dict suitable for `yt_dlp.YoutubeDL`.
  """
  parsed = yt_dlp.parse_options(
    [
      "--cookies-from-browser",
      cfg.cookie_browser,
      "--extractor-args",
      f"youtube:player_client={cfg.player_client}",
      "--format",
      cfg.format_chain,
    ]
  )
  opts = dict(parsed.ydl_opts)
  opts.update({"quiet": True, "no_warnings": True, "noprogress": True})
  return opts


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
  info = info or {}
  # a single video url has no "entries" — it *is* the entry. without this a
  # pasted track link silently yields nothing (cp3).
  entries = info.get("entries")
  if entries is None:
    entries = [info] if info.get("id") else []
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
