"""Command-line entry points (SPEC.md §18)."""

import argparse
import logging
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

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
from music.arbitrate import arbitrate, persist
from music.classify import family_for_identity
from music.identify import confidence, review_reason, should_auto_accept
from music.normalise import normalise
from music.publish import publish_track, retag
from music.publish.naming import extract_version
from music.resolve import Resolver
from music.sources import build_enrichment_sources
from music.sources.acoustid import AcoustId
from music.sources.base import FieldCandidate, Identity
from music.sources.musicbrainz import MusicBrainz

if TYPE_CHECKING:  # the classify group is optional and heavy
  from music.classify import Classifier, Prediction

log = logging.getLogger("music")

# loaded once; pulls in tensorflow and ~20 MB of graphs
_CLASSIFIER: Classifier | None = None
_CLASSIFY_WARNED = False


def _open(cfg: config.Config) -> sqlite3.Connection:
  conn = db.connect(cfg.paths.database)
  db.migrate(conn)
  return conn


def _classified_genre(path: Path) -> Prediction | None:
  """Top-level genre from the local classifier, for precedence routing.

  Returns an empty string when Essentia is not installed — the optional
  `classify` dependency group is heavy, and a run without it should degrade to
  the ISRC override plus the `other` family rather than fail.

  Args:
    path: Audio file.

  Returns:
    The top prediction, or None when Essentia is unavailable.
  """
  global _CLASSIFIER
  try:
    if _CLASSIFIER is None:
      from music.classify import Classifier

      _CLASSIFIER = Classifier()
    predictions = _CLASSIFIER.predict(path, top=1)
  except Exception as exc:  # noqa: BLE001 - routing degrades, never fails a run
    global _CLASSIFY_WARNED
    if not _CLASSIFY_WARNED:
      # warn once: silent degradation looks identical to "everything is
      # genuinely `other`", which is how this went unnoticed the first time.
      log.warning(
        "genre classification unavailable (%s); all tracks will route to the "
        "`other` precedence table. install with: uv sync --group classify",
        exc,
      )
      _CLASSIFY_WARNED = True
    return None
  return predictions[0] if predictions else None


def _resolve_and_arbitrate(
  conn: sqlite3.Connection,
  track_id: int,
  item: Download,
  resolver: Resolver,
) -> bool:
  """Run the full resolution path for one track and store the result.

  Args:
    conn: Open connection.
    track_id: Track being processed.
    item: The download, for its title and channel.
    resolver: Configured resolver.

  Returns:
    True if the identity was confident enough to publish (SPEC.md §12).
  """
  # yt-dlp gives channel and title; the channel is the better artist signal,
  # and normalise strips the VEVO/Topic noise and any redundant "Artist - "
  cleaned = normalise(item.channel, item.title)
  identity = Identity(
    artist=cleaned.artist,
    title=cleaned.title,
    artist_full=cleaned.artist_full,
    duration_s=item.duration_s or None,
  )
  result = resolver.resolve(identity, item.path)

  candidates = list(result.candidates)
  # remixer and mix name come from the title we already have (SPEC.md §14)
  version = extract_version(item.title)
  if version.mix_name:
    candidates.append(
      FieldCandidate(field="mix_name", value=version.mix_name, source="derived")
    )
  if version.remixer:
    candidates.append(
      FieldCandidate(field="remixer", value=version.remixer, source="derived")
    )

  # classification runs BEFORE candidates are stored, so essentia's genre is
  # recorded for provenance like any other source. appending it afterwards let
  # it win arbitration while never appearing in the review ui.
  #
  # the family is finalised here, not at classify time: the ISRC override
  # needs an ISRC, which only exists once identity is resolved (SPEC.md §6).
  #
  # the classifier's TOP-LEVEL genre is what routes, not the resolved genre
  # field — that holds a Discogs *style* ("Dance-pop", "Hip-House") which is
  # not in the family map and silently routed everything to `other`.
  by_field = {c.field: c.value for c in candidates}
  prediction = _classified_genre(item.path)
  family = family_for_identity(
    prediction.genre if prediction else "", by_field.get("isrc")
  )
  # essentia is the last-resort genre source (SPEC.md §7): it ranks below
  # every real source, but it describes *this audio* rather than whichever
  # release a catalogue happened to match.
  if prediction and prediction.style:
    candidates.append(
      FieldCandidate(
        field="genre",
        value=prediction.style,
        source="essentia",
        confidence=float(prediction.activation),
      )
    )
  conn.execute("UPDATE track SET genre_family=? WHERE id=?", (family, track_id))
  conn.execute(
    "UPDATE track SET identity_confidence=?, norm_artist=?, norm_title=?,"
    " acoustid=?, mb_recording_id=?, updated_at=datetime('now') WHERE id=?",
    (
      confidence(result.match),
      cleaned.artist,
      cleaned.title,
      result.acoustid or None,
      result.mb_recording_id or None,
      track_id,
    ),
  )
  for candidate in candidates:
    conn.execute(
      "INSERT INTO field_candidate (track_id, field, value, source, confidence)"
      " VALUES (?,?,?,?,?)",
      (
        track_id,
        candidate.field,
        candidate.value,
        candidate.source,
        candidate.confidence,
      ),
    )

  if not should_auto_accept(result.match):
    reason = review_reason(result.match) or "low_confidence"
    conn.execute(
      "INSERT OR IGNORE INTO review_queue (track_id, reason) VALUES (?,?)",
      (track_id, reason),
    )
    conn.execute("UPDATE track SET status='review' WHERE id=?", (track_id,))
    return False

  persist(conn, track_id, arbitrate(candidates, family=family))
  return True


def cmd_ingest(args: argparse.Namespace) -> int:
  """Download a playlist or single url and run it through the pipeline."""
  cfg = config.load()
  conn = _open(cfg)
  key = cfg.credentials.get("ACOUSTID_API_KEY", "")
  resolver = Resolver(
    conn,
    MusicBrainz(conn),
    AcoustId(conn, key) if key else None,
    build_enrichment_sources(conn, cfg),
  )
  refs = enumerate_playlist(args.url, cfg.youtube)
  if not refs:
    log.error("no entries found at %s", args.url)
    return 1

  if args.limit:
    refs = refs[: args.limit]
  log.info("found %d track(s)", len(refs))

  published = skipped = failed = queued = 0
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
      if not _resolve_and_arbitrate(conn, track_id, item, resolver):
        log.info("  review needed: %s", item.title[:52])
        queued += 1
        continue
      dest = publish_track(conn, track_id, cfg.paths.library, cfg.paths.staging)
      log.info("published: %s", dest.name)
      published += 1
    except Exception as exc:  # noqa: BLE001 - one bad track must not stop a run
      log.error("failed %s: %s", ref.title[:50], exc)
      failed += 1
  log.info(
    "done: %d published, %d queued for review, %d skipped, %d failed",
    published,
    queued,
    skipped,
    failed,
  )
  return 0 if failed == 0 else 1


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
