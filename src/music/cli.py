"""Command-line entry points (SPEC.md §18)."""

import argparse
import logging
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from music import config, db, pipeline
from music.acquire.youtube import cookie_path as youtube_cookie_path
from music.arbitrate import rearbitrate
from music.publish import refreshed_artwork, reset_refreshed, retag

if TYPE_CHECKING:  # the classify group is optional and heavy
  from music.classify import Classifier

log = logging.getLogger("music")

# loaded once; pulls in tensorflow and ~20 MB of graphs
_CLASSIFIER: Classifier | None = None
_CLASSIFY_WARNED = False


def _open(cfg: config.Config) -> sqlite3.Connection:
  conn = db.connect(cfg.paths.database)
  db.migrate(conn)
  return conn


def cmd_ingest(args: argparse.Namespace) -> int:
  """Download a playlist or single url and run it through the pipeline."""
  cfg = config.load()
  state = pipeline.ingest(args.url, cfg, limit=args.limit or 0)
  if state.error:
    log.error("%s", state.error)
    return 1
  return 0 if state.failed == 0 else 1


def cmd_retag(args: argparse.Namespace) -> int:
  """Re-emit tags from the database onto already-published files.

  With `--rearbitrate`, arbitration is re-run from the stored candidates
  first. This is the lever the database-as-source-of-truth design exists for
  (SPEC.md §11): a resolver improvement is applied to the whole library
  without re-downloading or re-querying anything. Manual edits are preserved,
  because `persist` never overwrites them.
  """
  cfg = config.load()
  conn = _open(cfg)
  if args.id:
    ids = [args.id]
  else:
    ids = [
      row["id"]
      for row in conn.execute(
        "SELECT id FROM track WHERE published_path IS NOT NULL ORDER BY id"
      )
    ]
  if not ids:
    log.info("nothing published yet")
    return 0

  if args.rearbitrate:
    changed = sum(rearbitrate(conn, track_id) for track_id in ids)
    log.info("re-arbitrated %d track(s), %d field(s) changed", len(ids), changed)

  reset_refreshed()
  done = missing = renamed = 0
  for track_id in ids:
    before = conn.execute(
      "SELECT published_path FROM track WHERE id = ?", (track_id,)
    ).fetchone()["published_path"]
    path = retag(conn, track_id)
    if path is None:
      missing += 1
      continue
    done += 1
    if str(path) != before:
      renamed += 1
      log.info("renamed: %s", path.name)
    log.debug("retagged %s", path.name)

  covers = len(refreshed_artwork())
  log.info(
    "retagged %d file(s): %d cover(s) replaced, %d renamed, %d missing",
    done,
    covers,
    renamed,
    missing,
  )
  # rekordbox caches tags per path and will not notice (SPEC.md §10)
  if done:
    log.warning("rekordbox caches tags per path — use Reload Tag to see changes")
  return 0


def _free_port(preferred: int, host: str = "127.0.0.1", tries: int = 20) -> int | None:
  """Find a bindable port, starting at `preferred`.

  Args:
    preferred: The port to try first.
    host: Interface to bind.
    tries: How many consecutive ports to try.

  Returns:
    A free port, or None if none of them are free.
  """
  import socket

  for port in range(preferred, preferred + tries):
    with socket.socket() as probe:
      probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      try:
        probe.bind((host, port))
      except OSError:
        continue
      return port
  return None


def cmd_serve(args: argparse.Namespace) -> int:
  """Run the local review and editing UI (SPEC.md §15)."""
  import uvicorn

  from music.web import create_app

  port = args.port
  if _free_port(port, tries=1) is None:
    if args.strict_port:
      log.error(
        "port %d is already in use. another `music serve` may still be running:"
        "\n  lsof -nP -iTCP:%d -sTCP:LISTEN",
        port,
        port,
      )
      return 1
    found = _free_port(port + 1)
    if found is None:
      log.error("no free port near %d", port)
      return 1
    log.warning("port %d is in use; using %d instead", port, found)
    port = found

  log.info("review ui: http://127.0.0.1:%d", port)
  if args.no_reload:
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
    return 0

  # Reload on source changes by default. This is a local tool that is edited
  # while it runs, and a server holding stale code is invisible: the ui saves
  # correctly, reports success, and the file never changes because the *old*
  # `retag` is still in memory. That cost hours once.
  uvicorn.run(
    "music.web:create_app",
    factory=True,
    host="127.0.0.1",
    port=port,
    log_level="warning",
    reload=True,
    reload_dirs=[str(Path(__file__).resolve().parent)],
  )
  return 0


def cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
  """Print counts by stage and status."""
  cfg = config.load()
  conn = _open(cfg)
  rows = conn.execute(
    "SELECT stage, status, count(*) n FROM track GROUP BY stage, status"
    " ORDER BY stage, status"
  ).fetchall()
  if not rows:
    print("no tracks yet")
    return 0
  for row in rows:
    print(f"  {row['stage']:<12} {row['status']:<10} {row['n']:>5}")
  return 0


def cmd_reset(args: argparse.Namespace) -> int:
  """Delete the library, staging files and database, for a clean run.

  The database is the source of truth and the audio is a regenerable
  projection (SPEC.md §11) — but a reset throws away every manual edit and
  every recorded calibration answer too, and those are not regenerable from
  anything. Hence the typed confirmation.

  Serato and rekordbox keep their own databases pointing at these paths; after
  a reset their libraries will reference files that no longer exist.
  """
  cfg = config.load()
  targets = [
    ("library", cfg.paths.library),
    ("staging", cfg.paths.staging),
    ("database", cfg.paths.database),
  ]
  if args.keep_staging:
    targets = [t for t in targets if t[0] != "staging"]

  print("this will permanently delete:")
  total_files = total_bytes = 0
  for label, path in targets:
    files = bytes_ = 0
    if path.is_dir():
      for item in path.rglob("*"):
        if item.is_file():
          files += 1
          bytes_ += item.stat().st_size
    elif path.is_file():
      files, bytes_ = 1, path.stat().st_size
    total_files += files
    total_bytes += bytes_
    print(f"  {label:<9} {files:>6} file(s)  {bytes_ / 1024**3:>6.2f} GB  {path}")

  if not total_files:
    print("nothing to delete")
    return 0

  counts = _reset_counts(cfg)
  if counts:
    print(
      f"\nthe database holds {counts['tracks']} track(s),"
      f" {counts['manual']} manual edit(s) and"
      f" {counts['answers']} calibration answer(s) — none of it recoverable."
    )

  if not args.yes:
    print(f'\ntype "delete {total_files}" to confirm: ', end="", flush=True)
    if sys.stdin.readline().strip() != f"delete {total_files}":
      print("aborted")
      return 1

  for label, path in targets:
    if path.is_dir():
      shutil.rmtree(path)
    elif path.is_file():
      path.unlink()
      # sqlite leaves these behind in wal mode; a stale -wal against a new
      # database is a corruption report waiting to happen
      for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
          sidecar.unlink()
    print(f"  removed {label}")
  print("reset complete; run `music ingest <url>` to start again")
  return 0


def _reset_counts(cfg: config.Config) -> dict[str, int] | None:
  """Count what a reset would destroy that nothing can rebuild.

  Args:
    cfg: Runtime configuration.

  Returns:
    Counts of tracks, manual edits and calibration answers, or None if there
    is no readable database.
  """
  if not cfg.paths.database.is_file():
    return None
  try:
    conn = db.connect(cfg.paths.database)
    one = conn.execute("SELECT count(*) n FROM track").fetchone()["n"]
    manual = conn.execute(
      "SELECT count(*) n FROM resolved_field WHERE decided_by = 'manual'"
    ).fetchone()["n"]
    answers = conn.execute("SELECT count(*) n FROM elicitation").fetchone()["n"]
    conn.close()
  except Exception:  # noqa: BLE001 - a broken database is one more reason to reset
    return None
  return {"tracks": one, "manual": manual, "answers": answers}


def cmd_cookies(args: argparse.Namespace) -> int:
  """Export YouTube cookies from a browser profile into a reusable file.

  No extension needed. The point is not *how* the cookies are read but *which
  profile* they are read from: YouTube rotates account cookies on open tabs, so
  a profile you browse YouTube in invalidates its own exports. A profile that
  is logged in and then left alone has nothing rotating it.

  `--list` shows the profiles; `--profile` picks one; the result is written to
  `youtube.cookie_file`, which the pipeline then uses verbatim and never
  refreshes (SPEC.md §6).
  """
  from yt_dlp.cookies import extract_cookies_from_browser

  cfg = config.load()
  if args.list:
    found = _chrome_profiles(cfg.youtube.cookie_browser)
    if not found:
      print(f"  no {cfg.youtube.cookie_browser} profiles found")
      return 1
    for directory, name in found:
      print(f"  {directory:<12} {name}")
    print("\n  a profile you browse YouTube in will keep invalidating itself;")
    print("  make a second one, sign in, and leave it closed.")
    return 0

  destination = (
    Path(args.out).expanduser() if args.out else youtube_cookie_path(cfg.youtube)
  )
  try:
    jar = extract_cookies_from_browser(cfg.youtube.cookie_browser, profile=args.profile)
  except Exception as exc:  # noqa: BLE001 - the message is the whole point
    print(f"  could not read {cfg.youtube.cookie_browser}/{args.profile}: {exc}")
    return 1

  # Only what the job needs. A whole-profile export puts every site the user is
  # signed into on disk in plain text; YouTube authentication lives on the
  # google.com and youtube.com domains (SPEC.md §13).
  for cookie in list(jar):
    if not any(d in (cookie.domain or "") for d in COOKIE_DOMAINS):
      jar.clear(cookie.domain, cookie.path, cookie.name)

  names = {c.name for c in jar if "youtube" in (c.domain or "")}
  authenticated = "LOGIN_INFO" in names and any(n.endswith("SAPISID") for n in names)
  destination.parent.mkdir(parents=True, exist_ok=True)
  jar.save(str(destination))
  destination.chmod(0o600)

  print(f"  wrote {len(jar)} cookies to {destination}")
  if not authenticated:
    print("  WARNING: no YouTube login found in that profile — sign in first")
    return 1
  print("  signed in to YouTube: yes")
  if str(destination) != cfg.youtube.cookie_file:
    print("\n  add this to ~/.config/musicpipeline/config.toml:")
    print("    [youtube]")
    print(f'    cookie_file = "{destination}"')
  return 0


# Domains carrying YouTube authentication. Every other site in the profile is
# unrelated to this job and is dropped before the file is written.
COOKIE_DOMAINS = ("youtube.com", "google.com", "ytimg.com", "googlevideo.com")

# where `music cookies` writes when nothing else is configured. Never the repo:
# an authenticated cookie jar is a credential (SPEC.md §13).
CONFIG_COOKIES = config.CONFIG_HOME / "youtube-cookies.txt"


def _chrome_profiles(browser: str) -> list[tuple[str, str]]:
  """List a Chromium browser's profile directories and their display names.

  Args:
    browser: Browser name as yt-dlp spells it.

  Returns:
    Pairs of directory name and display name.
  """
  import json

  roots = {
    "chrome": "Google/Chrome",
    "chromium": "Chromium",
    "brave": "BraveSoftware/Brave-Browser",
    "edge": "Microsoft Edge",
  }
  base = Path.home() / "Library" / "Application Support" / roots.get(browser, "")
  if not base.is_dir():
    return []
  out = []
  for directory in sorted(base.iterdir()):
    preferences = directory / "Preferences"
    if not preferences.is_file():
      continue
    try:
      name = json.loads(preferences.read_text()).get("profile", {}).get("name", "")
    except Exception:  # noqa: BLE001 - a name is cosmetic
      name = ""
    out.append((directory.name, name or "(unnamed)"))
  return out


def cmd_doctor(args: argparse.Namespace) -> int:
  """Check that the environment can actually run a job (SPEC.md §18)."""
  cfg = config.load()
  ok = True
  for tool in ("ffmpeg", "ffprobe", "fpcalc"):
    found = shutil.which(tool)
    print(f"  {tool:<10} {found or 'MISSING'}")
    ok &= bool(found)

  for key in ("ACOUSTID_API_KEY", "DISCOGS_TOKEN", "SPOTIFY_CLIENT_ID"):
    present = key in cfg.credentials
    print(f"  {key:<22} {'set' if present else 'not set'}")

  free_gb = shutil.disk_usage(Path.home()).free / 1024**3
  print(f"  free disk  {free_gb:.0f} GB (library needs ~80 GB)")
  ok &= free_gb > 100

  jar_path = youtube_cookie_path(cfg.youtube)
  state = "present" if jar_path.is_file() else "absent, will bootstrap"
  print(f"  cookies    {jar_path} ({state})")

  if not args.offline:
    ok &= _check_premium_audio(cfg)

  print("  ok" if ok else "  problems found")
  return 0 if ok else 1


# a short, stable, licensed art track used only to confirm the account still
# gets premium formats. nothing about it is kept.
PROBE_VIDEO = "u9W7FC0D-kg"


def _check_premium_audio(cfg: config.Config) -> bool:
  """Confirm the account still receives the premium audio format.

  yt-dlp now warns that `web_music` https formats require a GVS PO Token and
  "will be skipped". They are not skipped for a YouTube Premium subscriber —
  the token requirement does not apply — which is why itag 141 still arrives
  at ~256 kbps. That exemption is outside our control, so it is checked before
  a multi-hour run rather than discovered part way through it.

  The symptom if it ever changes is a downgrade to itag 140 at 128 kbps, which
  `download` already rejects against the bitrate floor.

  Args:
    cfg: Runtime configuration.

  Returns:
    True if the probe came back at or above the floor.
  """
  import tempfile

  from music.acquire import download, refresh_cookies

  refresh_cookies()
  with tempfile.TemporaryDirectory() as tmp:
    try:
      item = download(PROBE_VIDEO, Path(tmp), cfg.youtube)
    except Exception as exc:  # noqa: BLE001 - this is the diagnostic
      print(f"  premium audio  FAILED — {str(exc)[:90]}")
      return False
  good = item.abr >= cfg.youtube.min_bitrate_kbps
  verdict = "ok" if good else f"BELOW THE {cfg.youtube.min_bitrate_kbps} kbps FLOOR"
  print(f"  premium audio  itag {item.itag} at {item.abr:.0f} kbps ({verdict})")
  return good


def build_parser() -> argparse.ArgumentParser:
  """Construct the argument parser."""
  parser = argparse.ArgumentParser(prog="music", description=__doc__)
  parser.add_argument("-v", "--verbose", action="store_true")
  sub = parser.add_subparsers(dest="command", required=True)

  ingest = sub.add_parser("ingest", help="download a playlist and tag it")
  ingest.add_argument("url")
  ingest.add_argument("--limit", type=int, default=None)
  ingest.set_defaults(func=cmd_ingest)

  status = sub.add_parser("status", help="counts by stage and status")
  status.set_defaults(func=cmd_status)

  retag_cmd = sub.add_parser("retag", help="re-emit tags from the database")
  retag_cmd.add_argument(
    "--rearbitrate",
    action="store_true",
    help="re-run arbitration from stored candidates before re-tagging",
  )
  retag_cmd.add_argument("--id", type=int, default=None, help="a single track id")
  retag_cmd.set_defaults(func=cmd_retag)

  for parser_name, helptext in (
    ("serve", "open the review and editing web ui"),
    ("review", "alias for serve"),
  ):
    sp = sub.add_parser(parser_name, help=helptext)
    sp.add_argument(
      "--no-reload",
      action="store_true",
      help="do not restart when the source changes",
    )
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument(
      "--strict-port",
      action="store_true",
      help="fail if --port is taken instead of trying the next one",
    )
    sp.set_defaults(func=cmd_serve)

  reset = sub.add_parser(
    "reset", help="delete the library, staging and database and start over"
  )
  reset.add_argument("--yes", action="store_true", help="skip the typed confirmation")
  reset.add_argument(
    "--keep-staging",
    action="store_true",
    help="keep downloaded audio, so a rebuild needs no re-download",
  )
  reset.set_defaults(func=cmd_reset)

  cookies = sub.add_parser(
    "cookies", help="export youtube cookies from a browser profile"
  )
  cookies.add_argument("--list", action="store_true", help="list browser profiles")
  cookies.add_argument(
    "--profile", default="Default", help="profile directory name, e.g. 'Profile 1'"
  )
  cookies.add_argument("--out", default="", help="where to write the cookie file")
  cookies.set_defaults(func=cmd_cookies)

  doctor = sub.add_parser("doctor", help="check tools, credentials and disk")
  doctor.add_argument(
    "--offline",
    action="store_true",
    help="skip the download probe that confirms premium audio",
  )
  doctor.set_defaults(func=cmd_doctor)
  return parser


def main(argv: list[str] | None = None) -> int:
  """Run the cli.

  Args:
    argv: Arguments, defaulting to sys.argv.

  Returns:
    Process exit code.
  """
  args = build_parser().parse_args(argv)
  logging.basicConfig(
    level=logging.DEBUG if args.verbose else logging.INFO,
    format="%(message)s",
  )
  result: int = args.func(args)
  return result


if __name__ == "__main__":  # pragma: no cover
  sys.exit(main())
