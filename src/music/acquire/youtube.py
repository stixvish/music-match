"""Fetch audio from YouTube via the yt-dlp Python API.

The library is used rather than the CLI so the structured info dict is
available directly: `channel` and `description` drive art-track detection, and
`abr` drives the quality gate (SPEC.md §6).
"""

import logging
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
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


# yt-dlp's own test for a usable YouTube session (`_has_auth_cookies`):
# LOGIN_INFO plus a SAPISID variant. YouTube clears LOGIN_INFO when it
# de-authenticates a session, and leaves the SAPISID family behind, so the
# presence of SAPISID alone proves nothing.
def is_authenticated(jar: Path) -> bool:
  """Whether a cookie file carries a live YouTube login.

  Args:
    jar: Path to a Netscape-format cookie file.

  Returns:
    True if the file would authenticate. False if it is missing, unreadable,
    or has been de-authenticated.
  """
  if not jar.is_file() or jar.stat().st_size == 0:
    return False
  parsed = MozillaCookieJar(str(jar))
  try:
    parsed.load(ignore_discard=True, ignore_expires=True)
  except Exception:  # noqa: BLE001 - a corrupt jar is simply not authenticated
    return False
  names = {c.name for c in parsed if "youtube" in (c.domain or "")}
  return "LOGIN_INFO" in names and any("APISID" in n for n in names)


@contextmanager
def preserve_authentication(jar: Path | None) -> Iterator[None]:
  """Keep an authenticated jar authenticated across a yt-dlp run.

  yt-dlp saves the cookie jar back to `cookiefile` on exit, which is what keeps
  a rotating session current. But when YouTube *de-authenticates* the session
  it clears LOGIN_INFO, and writing that back destroys the export permanently:
  the next run no longer looks authenticated, so yt-dlp stops warning about it
  and quietly downloads a 128 kbps anonymous stream instead.

  Rotation is recoverable and must be persisted; de-authentication is not, and
  must not be. Measured against a live Chrome profile, a freshly exported jar
  lost LOGIN_INFO on its first request and itag 141 disappeared with it.

  Args:
    jar: The cookie file yt-dlp will write to, or None.

  Yields:
    None.
  """
  if jar is None or not is_authenticated(jar):
    yield
    return
  backup = jar.with_suffix(jar.suffix + ".authenticated")
  shutil.copy2(jar, backup)
  try:
    yield
  finally:
    if is_authenticated(jar):
      backup.unlink(missing_ok=True)
    else:
      shutil.copy2(backup, jar)
      backup.unlink(missing_ok=True)
      log.warning(
        "youtube de-authenticated the session mid-run and the jar was kept "
        "rather than overwritten. re-export with `music cookies` from a "
        "profile that has no youtube tab open: %s",
        jar,
      )


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

    extracted = extract_cookies_from_browser(
      cfg.cookie_browser, profile=cfg.cookie_profile or None
    )
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


def verify_cookies(jar: Path, cfg: YouTubeConfig, video_id: str) -> tuple[bool, str]:
  """Prove a cookie jar authenticates, instead of assuming that it will.

  A jar can hold every cookie a login needs and still be dead on arrival. If
  the profile it was exported from has a live YouTube session, YouTube rotates
  on the very first request, clears LOGIN_INFO, and the premium formats go with
  it. Nothing about the file itself reveals this — checking that the cookies
  are *present* at export time passes, and the failure only surfaces later as a
  download rejected against the bitrate floor.

  The probe runs against a copy, so verifying a jar can never damage it.

  Args:
    jar: The cookie file to check.
    cfg: YouTube settings.
    video_id: A video to probe with. Metadata only; nothing is downloaded.

  Returns:
    Whether the jar authenticates, and a one-line reason either way.
  """
  if not is_authenticated(jar):
    return False, "no youtube login in the file"

  probe = jar.with_suffix(jar.suffix + ".probe")
  shutil.copy2(jar, probe)
  opts = _base_opts(cfg) | {
    "cookiefile": str(probe),
    "skip_download": True,
    "quiet": True,
    "no_warnings": True,
    # no format chain: selection would fail before the formats can be
    # inspected, reporting "Requested format is not available" instead of the
    # reason itag 141 is missing. That reason is the whole point here.
    "format": None,
  }
  info: dict | None = None
  failure: Exception | None = None
  try:
    with yt_dlp.YoutubeDL(opts) as ydl:
      info = ydl.extract_info(
        f"https://www.youtube.com/watch?v={video_id}", download=False
      )
  except Exception as exc:  # noqa: BLE001 - the message is the diagnostic
    failure = exc
  survived = is_authenticated(probe)
  probe.unlink(missing_ok=True)

  if failure is not None:
    return False, f"probe failed: {str(failure)[:120]}"
  if not survived:
    return False, (
      "youtube cleared the login on the first request — the profile it came "
      "from has a live youtube session that rotated it"
    )
  offered = {str(f.get("format_id") or "") for f in (info or {}).get("formats") or []}
  if not any(f.startswith("141") for f in offered):
    return False, "authenticated, but itag 141 (premium audio) was not offered"
  return True, "authenticated; itag 141 offered"


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

  # the file yt-dlp itself will write back to, as it resolved it
  written_jar = opts.get("cookiefile")
  try:
    with (
      preserve_authentication(Path(str(written_jar)) if written_jar else None),
      yt_dlp.YoutubeDL(opts) as ydl,
    ):
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
