"""Fetch audio from YouTube via the yt-dlp Python API.

The library is used rather than the CLI so the structured info dict is
available directly: `channel` and `description` drive art-track detection, and
`abr` drives the quality gate (SPEC.md §6).
"""

import atexit
import logging
import tempfile
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


_COOKIE_JAR: Path | None = None


def refresh_cookies() -> None:
  """Discard the extracted jar so the next call reads the browser again.

  Called at the start of every run. YouTube rotates session cookies, so a jar
  extracted once and kept for the life of the process goes stale — and
  `music serve` is a process that stays up for days. The previous version
  cached per *process* while its own docstring claimed per *run*.
  """
  global _COOKIE_JAR
  if _COOKIE_JAR is not None:
    _COOKIE_JAR.unlink(missing_ok=True)
  _COOKIE_JAR = None


def _cookie_jar(cfg: YouTubeConfig) -> Path | None:
  """Extract the browser's cookies once per run, into a private file.

  `--cookies-from-browser` re-reads the browser's cookie store on *every*
  yt-dlp call. On macOS that means unlocking the keychain and decrypting the
  whole store once per track, which is slow, prompts the user, and reads far
  more of their browsing data than the job needs.

  Once per *run* is the right unit: cheap enough to be invisible (0.15 s
  measured), and fresh enough that rotation between runs is picked up.
  `refresh_cookies` is what makes a run a run.

  The jar lives in a private temp file, mode 600, deleted at exit. It is never
  written into the repository or the config directory — it is an authenticated
  credential (SPEC.md §13).

  Args:
    cfg: YouTube settings, for which browser to read.

  Returns:
    Path to the cookie file, or None if extraction failed — in which case the
    caller falls back to per-call extraction rather than losing authentication.
  """
  global _COOKIE_JAR
  if _COOKIE_JAR is not None and _COOKIE_JAR.exists():
    return _COOKIE_JAR
  try:
    from yt_dlp.cookies import extract_cookies_from_browser

    jar = extract_cookies_from_browser(cfg.cookie_browser)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - lives for the run
      prefix="music-cookies-", suffix=".txt", delete=False
    )
    handle.close()
    path = Path(handle.name)
    path.chmod(0o600)
    jar.save(str(path))
  except Exception as exc:  # noqa: BLE001 - fall back rather than lose auth
    log.warning("could not pre-extract cookies (%s); falling back per call", exc)
    return None

  log.info("extracted %d cookies from %s", len(jar), cfg.cookie_browser)
  atexit.register(lambda: path.unlink(missing_ok=True))
  _COOKIE_JAR = path
  return path


def _base_opts(cfg: YouTubeConfig, sink: object | None = None) -> dict[str, object]:
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
    sink: Optional logger object with yt-dlp's `debug`/`info`/`warning`/
      `error` methods and an optional `hook` for progress callbacks.

  Returns:
    An options dict suitable for `yt_dlp.YoutubeDL`.
  """
  cookies = _cookie_jar(cfg)
  parsed = yt_dlp.parse_options(
    [
      *(
        ["--cookies", str(cookies)]
        if cookies
        else ["--cookies-from-browser", cfg.cookie_browser]
      ),
      "--extractor-args",
      f"youtube:player_client={cfg.player_client}",
      "--format",
      cfg.format_chain,
    ]
  )
  opts = dict(parsed.ydl_opts)
  opts.update({"quiet": True, "no_warnings": True, "noprogress": True})
  if sink is not None:
    # yt-dlp writes through a logger object rather than stdout when given one,
    # which is how its real output reaches the web ui instead of a terminal
    # nobody is watching (SPEC.md §15).
    opts["logger"] = sink
    # `noprogress` stays on: with it off, yt-dlp writes a "[download] 12.5%"
    # line through the logger several times a second and buries every real
    # event within seconds. The percentage comes from the hook instead, which
    # rewrites one line in place the way a terminal does.
    opts["progress_hooks"] = [getattr(sink, "hook", lambda _d: None)]
  return opts


def enumerate_playlist(
  url: str, cfg: YouTubeConfig, sink: object | None = None
) -> list[VideoRef]:
  """List a playlist without downloading anything.

  Args:
    url: Playlist URL.
    cfg: YouTube settings.
    sink: Optional yt-dlp logger, to mirror its output into the ui.

  Returns:
    One VideoRef per entry, skipping unavailable items.
  """
  opts = _base_opts(cfg, sink) | {"extract_flat": "in_playlist", "skip_download": True}
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


def download(
  video_id: str, dest: Path, cfg: YouTubeConfig, sink: object | None = None
) -> Download:
  """Fetch one track's audio.

  Args:
    video_id: YouTube video id.
    dest: Directory to write into.
    cfg: YouTube settings.
    sink: Optional yt-dlp logger, to mirror its output into the ui.

  Returns:
    A Download describing the result.

  Raises:
    QualityError: If the best available stream is below the bitrate floor.
    RuntimeError: If yt-dlp returns no usable info.
  """
  dest.mkdir(parents=True, exist_ok=True)
  opts = _base_opts(cfg, sink) | {"outtmpl": str(dest / "%(id)s.%(ext)s")}
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
