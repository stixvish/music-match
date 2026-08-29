"""Queries over the track index.

Everything here takes an open connection rather than opening its own, so
a caller can run a whole scan in one transaction and have a partial run
leave the database consistent.
"""

import pathlib
import sqlite3
from typing import Iterator


def upsert_track(
    connection: sqlite3.Connection,
    *,
    path: pathlib.Path,
    source_name: str,
    fingerprint: str | None = None,
    duration_seconds: float | None = None,
) -> int:
  """Records a track, updating it if the path is already known.

  Args:
    connection: An open connection.
    path: The file's path, which identifies it.
    source_name: The configured source folder it belongs to.
    fingerprint: Its encoded fingerprint, if computed.
    duration_seconds: Its duration, if known.

  Returns:
    The track's row id.
  """
  cursor = connection.execute(
      "INSERT INTO tracks (path, source_name, fingerprint, duration_seconds)"
      " VALUES (?, ?, ?, ?)"
      " ON CONFLICT(path) DO UPDATE SET"
      "   source_name = excluded.source_name,"
      "   fingerprint = coalesce(excluded.fingerprint, tracks.fingerprint),"
      "   duration_seconds ="
      "     coalesce(excluded.duration_seconds, tracks.duration_seconds),"
      "   updated_at = datetime('now')"
      " RETURNING id", (str(path), source_name, fingerprint, duration_seconds))
  row = cursor.fetchone()
  return int(row["id"])


def track_id_for_path(connection: sqlite3.Connection,
                      path: pathlib.Path) -> int | None:
  """Looks up a track by path.

  Args:
    connection: An open connection.
    path: The file's path.

  Returns:
    The row id, or None if the path is not indexed.
  """
  row = connection.execute("SELECT id FROM tracks WHERE path = ?",
                           (str(path),)).fetchone()
  return None if row is None else int(row["id"])


def fingerprinted_tracks(
    connection: sqlite3.Connection,
    source_name: str | None = None) -> Iterator[sqlite3.Row]:
  """Yields every track that has a fingerprint recorded.

  Args:
    connection: An open connection.
    source_name: Restrict to one source folder, or None for all.

  Yields:
    Rows with id, path, fingerprint, and duration_seconds.
  """
  sql = ("SELECT id, path, source_name, fingerprint, duration_seconds"
         " FROM tracks WHERE fingerprint IS NOT NULL")
  parameters: tuple[str, ...] = ()
  if source_name is not None:
    sql += " AND source_name = ?"
    parameters = (source_name,)
  yield from connection.execute(sql + " ORDER BY path", parameters)


def paths_missing_fingerprints(connection: sqlite3.Connection) -> set[str]:
  """Returns the paths of indexed tracks that have no fingerprint yet.

  Args:
    connection: An open connection.

  Returns:
    The paths, as stored.
  """
  rows = connection.execute(
      "SELECT path FROM tracks WHERE fingerprint IS NULL").fetchall()
  return {row["path"] for row in rows}


def fingerprinted_paths(connection: sqlite3.Connection) -> set[str]:
  """Returns the paths of tracks already fingerprinted.

  Used to make a scan resumable: a run over 2000 files that dies partway
  should not redo the work it already did.

  Args:
    connection: An open connection.

  Returns:
    The paths, as stored.
  """
  rows = connection.execute(
      "SELECT path FROM tracks WHERE fingerprint IS NOT NULL").fetchall()
  return {row["path"] for row in rows}


def move_track(connection: sqlite3.Connection, track_id: int,
               new_path: pathlib.Path) -> None:
  """Records that a track's file has moved.

  Args:
    connection: An open connection.
    track_id: The track's row id.
    new_path: Where the file now lives.
  """
  connection.execute(
      "UPDATE tracks SET path = ?, updated_at = datetime('now')"
      " WHERE id = ?", (str(new_path), track_id))


def delete_track(connection: sqlite3.Connection, track_id: int) -> None:
  """Removes a track from the index.

  Its `download_archive` entry deliberately survives (the foreign key is
  ON DELETE SET NULL), so a file removed from the library is not
  downloaded again.

  Args:
    connection: An open connection.
    track_id: The track's row id.
  """
  connection.execute("DELETE FROM tracks WHERE id = ?", (track_id,))


def count_tracks(connection: sqlite3.Connection) -> int:
  """Returns how many tracks are indexed.

  Args:
    connection: An open connection.

  Returns:
    The row count.
  """
  row = connection.execute("SELECT count(*) AS n FROM tracks").fetchone()
  return int(row["n"])
