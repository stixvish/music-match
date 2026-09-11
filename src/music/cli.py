"""Command-line entry points (SPEC.md §18)."""

import argparse
import logging
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from music import config, db, pipeline
from music.publish import retag

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
  """Re-emit tags from the database onto already-published files."""
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

  done = missing = 0
  for track_id in ids:
    path = retag(conn, track_id)
    if path is None:
      missing += 1
      continue
    done += 1
    log.debug("retagged %s", path.name)
  log.info("retagged %d file(s), %d missing", done, missing)
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
  uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
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


def cmd_doctor(args: argparse.Namespace) -> int:  # noqa: ARG001
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
  print("  ok" if ok else "  problems found")
  return 0 if ok else 1


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
  retag_cmd.add_argument("--id", type=int, default=None, help="a single track id")
  retag_cmd.set_defaults(func=cmd_retag)

  for parser_name, helptext in (
    ("serve", "open the review and editing web ui"),
    ("review", "alias for serve"),
  ):
    sp = sub.add_parser(parser_name, help=helptext)
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

  doctor = sub.add_parser("doctor", help="check tools, credentials and disk")
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
