"""Acquisition: get audio onto disk and registered in the database."""

import json
import sqlite3
from pathlib import Path

from music.acquire import probe as probe_mod
from music.acquire.youtube import (
  Download,
  QualityError,
  VideoRef,
  download,
  enumerate_playlist,
)

__all__ = [
  "Download",
  "QualityError",
  "VideoRef",
  "already_have",
  "download",
  "enumerate_playlist",
  "register",
  "register_local",
]


def already_have(conn: sqlite3.Connection, video_id: str) -> bool:
  """Whether this video has been acquired before.

  Checked *before* downloading, so re-running a playlist of 300 tracks where
  280 are known costs twenty downloads, not three hundred (SPEC.md §8).

  Args:
    conn: Open connection.
    video_id: YouTube video id.

  Returns:
    True if a source_file row already exists.
  """
  row = conn.execute(
    "SELECT 1 FROM source_file WHERE video_id = ?", (video_id,)
  ).fetchone()
  return row is not None


def _insert(
  conn: sqlite3.Connection,
  *,
  origin: str,
  staging_path: Path,
  video_id: str | None = None,
  local_path: Path | None = None,
  channel: str = "",
  description: str = "",
  itag: str = "",
) -> int:
  info = probe_mod.probe(staging_path)
  cursor = conn.execute(
    "INSERT INTO source_file"
    " (origin, video_id, local_path, staging_path, sha256, duration_s,"
    "  codec, bitrate, sample_rate, itag, channel, description, raw_tags)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (
      origin,
      video_id,
      str(local_path) if local_path else None,
      str(staging_path),
      probe_mod.sha256(staging_path),
      info.duration_s,
      info.codec,
      info.bitrate,
      info.sample_rate,
      itag,
      channel,
      description,
      json.dumps(info.tags),
    ),
  )
  source_id = int(cursor.lastrowid or 0)
  track = conn.execute(
    "INSERT INTO track (source_file_id, stage) VALUES (?, 'acquired')",
    (source_id,),
  )
  return int(track.lastrowid or 0)


def register(conn: sqlite3.Connection, item: Download) -> int:
  """Record a completed download.

  Args:
    conn: Open connection.
    item: The download to register.

  Returns:
    The new track id.
  """
  return _insert(
    conn,
    origin="youtube",
    staging_path=item.path,
    video_id=item.video_id,
    channel=item.channel,
    description=item.description,
    itag=item.itag,
  )


def register_local(conn: sqlite3.Connection, path: Path) -> int:
  """Record a local file (beatport wavs, soundcloud) — SPEC.md §5.

  Args:
    conn: Open connection.
    path: Audio file already on disk.

  Returns:
    The new track id.
  """
  return _insert(conn, origin="local", staging_path=path, local_path=path)
