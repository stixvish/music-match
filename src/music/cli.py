"""Command-line entry points (SPEC.md §18)."""

import argparse
import logging
import shutil
import sqlite3
import sys
from pathlib import Path

from music import config, db
from music.acquire import (
  Download,
  already_have,
  classify_download,
  download,
  enumerate_playlist,
  flag_video_rip,
  register,
)
from music.publish import publish_track

log = logging.getLogger("music")


def _open(cfg: config.Config) -> sqlite3.Connection:
  conn = db.connect(cfg.paths.database)
  db.migrate(conn)
  return conn


def _stub_resolve(conn: sqlite3.Connection, track_id: int, item: Download) -> None:
  """Phase-0 placeholder for real resolution.

  Seeds artist and title from what yt-dlp reports, splitting an "Artist -
  Title" video title when present. This is deliberately naive: it is replaced
  wholesale in phase 1 by normalisation plus source lookup (tasks/todo.md
  t8-t11), which is where the accuracy actually comes from.

  Values are written as `precedence` so a later resolution run may overwrite
  them. Manual edits never are (SPEC.md §15).

  Args:
    conn: Open connection.
    track_id: Track to seed.
    item: The download yt-dlp produced.
  """
  title = item.title.strip()
  artist = item.channel.removesuffix(" - Topic").strip()
  if " - " in title:
    head, _, tail = title.partition(" - ")
    if head.strip() and tail.strip():
      artist, title = head.strip(), tail.strip()

  for field, value in (("artist", artist), ("title", title)):
    if value:
      conn.execute(
        "INSERT OR REPLACE INTO resolved_field"
        " (track_id, field, value, source, decided_by)"
        " VALUES (?,?,?,'yt-dlp','precedence')",
        (track_id, field, value),
      )


def cmd_ingest(args: argparse.Namespace) -> int:
  """Download a playlist or single url and run it through the pipeline."""
  cfg = config.load()
  conn = _open(cfg)
  refs = enumerate_playlist(args.url, cfg.youtube)
  if not refs:
    log.error("no entries found at %s", args.url)
    return 1

  if args.limit:
    refs = refs[: args.limit]
  log.info("found %d track(s)", len(refs))

  published = skipped = failed = 0
  for ref in refs:
    if already_have(conn, ref.video_id):
      log.info("skip (already have): %s", ref.title[:60])
      skipped += 1
      continue
    try:
      item = download(ref.video_id, cfg.paths.staging, cfg.youtube)
      track_id = register(conn, item)
      verdict = classify_download(item)
      flag_video_rip(conn, track_id, verdict)
      if verdict.is_video_rip:
        log.warning("  video rip (%s): %s", ",".join(verdict.reasons), item.title[:44])
      _stub_resolve(conn, track_id, item)
      dest = publish_track(conn, track_id, cfg.paths.library, cfg.paths.staging)
      log.info("published: %s", dest.name)
      published += 1
    except Exception as exc:  # noqa: BLE001 - one bad track must not stop a run
      log.error("failed %s: %s", ref.title[:50], exc)
      failed += 1
  log.info("done: %d published, %d skipped, %d failed", published, skipped, failed)
  return 0 if failed == 0 else 1


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
