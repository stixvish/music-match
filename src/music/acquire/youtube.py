"""Fetch audio from YouTube via the yt-dlp Python API.

The library is used rather than the CLI so the structured info dict is
available directly: `channel` and `description` drive art-track detection, and
`abr` drives the quality gate (SPEC.md §6).
"""

import logging
import re
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


def refresh_cookies() -> None:
  """Re-read the browser, replacing the stored jar.

  **Not called per run.** yt-dlp writes rotated cookies back to a cookie file
  on exit (`YoutubeDL.save_cookies`), so a persistent jar keeps itself current
  as YouTube rotates the session. Discarding it every run and re-reading the
  browser threw those fresh cookies away and went back to the browser's stale
  copy — which is what produced "The provided YouTube account cookies are no
  longer valid" on a machine where nothing else was touching YouTube.

  This is now the recovery path only: called when a download is downgraded
  below the bitrate floor, and by `music cookies` when the user asks for it.
  """
  jar = cookie_path()
  jar.unlink(missing_ok=True)


def cookie_path(cfg: YouTubeConfig | None = None) -> Path:
  """Where the persistent cookie jar lives.

  Args:
    cfg: YouTube settings; `cookie_file` overrides the default location.

  Returns:
    The jar path. It is never inside the repository — an authenticated jar is
    a credential (SPEC.md §13).
  """
  if cfg is not None and cfg.cookie_file:
    return Path(cfg.cookie_file).expanduser()
  return Path.home() / ".config" / "musicpipeline" / "youtube-cookies.txt"


def _cookie_jar(cfg: YouTubeConfig) -> Path | None:
  """Return a cookie file for yt-dlp, creating it from the browser if needed.

  A **file**, not `--cookies-from-browser`, because yt-dlp saves the jar back
  to a cookiefile when it exits. YouTube rotates the session during a run; with
  a file those rotations are persisted and the next run starts from current
  cookies. With `--cookies-from-browser` they are discarded and the browser's
  older copy is read again, which eventually fails authentication even when the
  browser is never used for YouTube at all.

  Reading the browser is therefore a bootstrap, not a routine: it happens once,
  when no jar exists yet.

  Args:
    cfg: YouTube settings.

  Returns:
    Path to the cookie file, or None if it could not be created — in which case
    the caller falls back to per-call browser extraction rather than losing
    authentication entirely.
  """
  jar = cookie_path(cfg)
  if jar.is_file() and jar.stat().st_size > 0:
    return jar

  try:
    from yt_dlp.cookies import extract_cookies_from_browser

    extracted = extract_cookies_from_browser(cfg.cookie_browser)
    jar.parent.mkdir(parents=True, exist_ok=True)
    extracted.save(str(jar))
    jar.chmod(0o600)
  except Exception as exc:  # noqa: BLE001 - fall back rather than lose auth
    log.warning("could not read %s cookies (%s)", cfg.cookie_browser, exc)
    return None

  log.info(
    "bootstrapped %d cookies from %s into %s", len(extracted), cfg.cookie_browser, jar
  )
  return jar


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
  # `ignoreerrors` comes back as "only_download" from yt-dlp's CLI defaults,
  # which swallows every download failure and returns None instead. Combined
  # with `quiet`, a 403, a geo-block and an expired cookie all surfaced here as
  # the same useless "yt-dlp returned nothing" — which reads exactly like dead
  # authentication and is usually not. Let the real error through.
  opts.update(
    {"quiet": True, "no_warnings": True, "noprogress": True, "ignoreerrors": False}
  )
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


def _clean_error(exc: Exception) -> str:
  """Strip yt-dlp's ANSI codes and prefix so the message reads in a log.

  Args:
    exc: The raised error.

  Returns:
    A single readable line.
  """
  text = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
  text = re.sub(r"^ERROR:\s*", "", text).strip()
  return " ".join(text.split())[:300]


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

  try:
    with yt_dlp.YoutubeDL(opts) as ydl:
      info = ydl.extract_info(url, download=True)
  except yt_dlp.utils.DownloadError as exc:
    # carry yt-dlp's own words: they name the cause, and the caller logs them
    raise RuntimeError(f"{video_id}: {_clean_error(exc)}") from exc
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
