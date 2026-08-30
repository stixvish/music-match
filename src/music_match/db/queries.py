"""Queries over the track index.

Everything here takes an open connection rather than opening its own, so
a caller can run a whole scan in one transaction and have a partial run
leave the database consistent.
"""

import pathlib
import sqlite3
from typing import Any, Iterator, Mapping


def upsert_track(
    connection: sqlite3.Connection,
    *,
    path: pathlib.Path,
    source_name: str,
    fingerprint: str | None = None,
    duration_seconds: float | None = None,
    detected_genre: str | None = None,
    genre_confidence: float | None = None,
) -> int:
  """Records a track, updating it if the path is already known.

  Every optional column is coalesced rather than overwritten, so a caller
  that knows only one of them — the fingerprint scan, or the genre pass —
  fills in its own field without clearing the others.

  Args:
    connection: An open connection.
    path: The file's path, which identifies it.
    source_name: The configured source folder it belongs to.
    fingerprint: Its encoded fingerprint, if computed.
    duration_seconds: Its duration, if known.
    detected_genre: Its locally-detected genre label, if computed.
    genre_confidence: How strongly the model backed that label. Stored
      alongside it because the label alone is not worth much — see
      db.schema.

  Returns:
    The track's row id.
  """
  cursor = connection.execute(
      "INSERT INTO tracks"
      "   (path, source_name, fingerprint, duration_seconds, detected_genre,"
      "    genre_confidence)"
      " VALUES (?, ?, ?, ?, ?, ?)"
      " ON CONFLICT(path) DO UPDATE SET"
      "   source_name = excluded.source_name,"
      "   fingerprint = coalesce(excluded.fingerprint, tracks.fingerprint),"
      "   duration_seconds ="
      "     coalesce(excluded.duration_seconds, tracks.duration_seconds),"
      "   detected_genre ="
      "     coalesce(excluded.detected_genre, tracks.detected_genre),"
      "   genre_confidence ="
      "     coalesce(excluded.genre_confidence, tracks.genre_confidence),"
      "   updated_at = datetime('now')"
      " RETURNING id", (str(path), source_name, fingerprint, duration_seconds,
                        detected_genre, genre_confidence))
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


def record_match(
    connection: sqlite3.Connection,
    *,
    track_id: int,
    source: str | None,
    confidence: float,
    status: str,
    tags_json: str | None,
    art_url: str | None,
) -> None:
  """Stores a proposed match against a track.

  Writes the proposal only — the file itself is untouched until tag
  writing applies it.

  Args:
    connection: An open connection.
    track_id: The track's row id.
    source: The source that supplied the most fields, if any.
    confidence: How much to trust the match.
    status: The resulting match status.
    tags_json: The proposed tags, JSON-encoded.
    art_url: The cover image to embed, if one was found.
  """
  connection.execute(
      "UPDATE tracks SET matched_source = ?, match_confidence = ?,"
      "  matched_tags_json = ?, matched_art_url = ?, match_status = ?,"
      "  matched_at = datetime('now'), updated_at = datetime('now')"
      " WHERE id = ?",
      (source, confidence, tags_json, art_url, status, track_id))


def matched_paths(connection: sqlite3.Connection) -> set[str]:
  """Returns the paths of tracks that already have a match recorded.

  Args:
    connection: An open connection.

  Returns:
    The paths, as stored.
  """
  rows = connection.execute(
      "SELECT path FROM tracks WHERE matched_at IS NOT NULL").fetchall()
  return {row["path"] for row in rows}


def match_status_counts(
    connection: sqlite3.Connection) -> list[tuple[str, int]]:
  """Summarises how many tracks are in each match state.

  Args:
    connection: An open connection.

  Returns:
    (status, count) pairs, most common first.
  """
  rows = connection.execute(
      "SELECT match_status AS status, count(*) AS n FROM tracks"
      " GROUP BY match_status ORDER BY n DESC, status").fetchall()
  return [(str(row["status"]), int(row["n"])) for row in rows]


def tracks_for_matching(
    connection: sqlite3.Connection,
    source_name: str | None = None) -> Iterator[sqlite3.Row]:
  """Yields indexed tracks with what is known about them.

  Args:
    connection: An open connection.
    source_name: Restrict to one source folder, or None for all.

  Yields:
    Rows with id, path, duration and detected genre.
  """
  sql = (
      "SELECT id, path, source_name, duration_seconds, detected_genre,"
      "       genre_confidence, matched_at"
      " FROM tracks"
      " WHERE id NOT IN (SELECT track_id FROM wont_match)"
      # A quarantined file is waiting on a human, and matching a music
      # video's audio is the API call this whole check exists to save.
      "   AND match_status != 'quarantined'")
  parameters: tuple[str, ...] = ()
  if source_name is not None:
    sql += " AND source_name = ?"
    parameters = (source_name,)
  yield from connection.execute(sql + " ORDER BY path", parameters)


def record_snapshot(connection: sqlite3.Connection, track_id: int,
                    tags_json: str) -> None:
  """Stores what a file's tags currently are.

  Distinct from `matched_tags_json`, which holds what they *should*
  become. Keeping the two apart is what lets `reindex` tell a library
  that lost its database from one that was never tagged.

  Args:
    connection: An open connection.
    track_id: The track's row id.
    tags_json: The file's current tags, JSON-encoded.
  """
  connection.execute(
      "UPDATE tracks SET tags_json = ?, updated_at = datetime('now')"
      " WHERE id = ?", (tags_json, track_id))


def archive_size(connection: sqlite3.Connection) -> int:
  """Returns how many source ids the download archive holds.

  Args:
    connection: An open connection.

  Returns:
    The row count.
  """
  row = connection.execute(
      "SELECT count(*) AS n FROM download_archive").fetchone()
  return int(row["n"])


def tracks_with_matches(
    connection: sqlite3.Connection,
    source_name: str | None = None) -> Iterator[sqlite3.Row]:
  """Yields tracks that have a recorded match proposal.

  Args:
    connection: An open connection.
    source_name: Restrict to one source folder, or None for all.

  Yields:
    Rows with the proposal and the status it was given.
  """
  sql = ("SELECT id, path, match_status, match_confidence,"
         "       matched_tags_json, matched_art_url"
         " FROM tracks WHERE matched_tags_json IS NOT NULL")
  parameters: tuple[str, ...] = ()
  if source_name is not None:
    sql += " AND source_name = ?"
    parameters = (source_name,)
  yield from connection.execute(sql + " ORDER BY path", parameters)


def set_match_status(connection: sqlite3.Connection, track_id: int,
                     status: str) -> None:
  """Sets a track's match status.

  Args:
    connection: An open connection.
    track_id: The track's row id.
    status: The new status, from the schema's allowed set.
  """
  connection.execute(
      "UPDATE tracks SET match_status = ?, updated_at = datetime('now')"
      " WHERE id = ?", (status, track_id))


def quarantined_tracks(connection: sqlite3.Connection) -> Iterator[sqlite3.Row]:
  """Yields tracks currently held for a human decision.

  Args:
    connection: An open connection.

  Yields:
    Rows with id and path.
  """
  yield from connection.execute(
      "SELECT id, path, source_name FROM tracks"
      " WHERE match_status = 'quarantined' ORDER BY path")


def mark_wont_match(connection: sqlite3.Connection,
                    track_id: int,
                    reason: str | None = None) -> None:
  """Marks a track as never going to match, so it stops being flagged.

  For self-made edits and unofficial uploads that no public database will
  ever hold.

  Args:
    connection: An open connection.
    track_id: The track's row id.
    reason: An optional note about why.
  """
  connection.execute(
      "INSERT INTO wont_match (track_id, reason) VALUES (?, ?)"
      " ON CONFLICT(track_id) DO UPDATE SET reason = excluded.reason",
      (track_id, reason))
  connection.execute(
      "UPDATE tracks SET match_status = 'wont_match',"
      " updated_at = datetime('now') WHERE id = ?", (track_id,))


def genre_tagged_paths(connection: sqlite3.Connection) -> set[str]:
  """Returns the paths of tracks that already have a detected genre.

  Used to make the genre pass resumable, the same way `fingerprinted_paths`
  does for the fingerprint scan.

  Args:
    connection: An open connection.

  Returns:
    The paths, as stored.
  """
  rows = connection.execute(
      "SELECT path FROM tracks WHERE detected_genre IS NOT NULL").fetchall()
  return {row["path"] for row in rows}


def detected_genre_counts(
    connection: sqlite3.Connection,
    minimum_confidence: float = 0.0) -> list[tuple[str, int, float]]:
  """Summarises how many tracks fell into each detected genre.

  Args:
    connection: An open connection.
    minimum_confidence: Ignore labels the model backed less strongly than
      this.

  Returns:
    (label, count, mean confidence) triples, most common first.
  """
  rows = connection.execute(
      "SELECT detected_genre AS label, count(*) AS n,"
      "       avg(coalesce(genre_confidence, 0)) AS mean"
      " FROM tracks"
      " WHERE detected_genre IS NOT NULL"
      "   AND coalesce(genre_confidence, 0) >= ?"
      " GROUP BY detected_genre ORDER BY n DESC, label",
      (minimum_confidence,)).fetchall()
  return [(str(row["label"]), int(row["n"]), float(row["mean"])) for row in rows
         ]


def log_changes(connection: sqlite3.Connection, *, track_id: int, batch: str,
                changes: Mapping[str, tuple[Any, Any]]) -> None:
  """Records tag changes before they are written to the file.

  Called *before* the write, so an interrupted run leaves history for a
  change that may not have happened rather than a change with no history.
  The first is recoverable; the second silently loses the old value
  forever.

  Args:
    connection: An open connection.
    track_id: The track's row id.
    batch: An identifier shared by every row from this one write.
    changes: Field name to (old value, new value).
  """
  connection.executemany(
      "INSERT INTO tag_history (track_id, batch, field, old_value, new_value)"
      " VALUES (?, ?, ?, ?, ?)",
      [(track_id, batch, field, _as_text(old), _as_text(new))
       for field, (old, new) in changes.items()])


def _as_text(value: Any) -> str | None:
  """Renders a tag value for storage in the history table.

  Args:
    value: The value, of whatever type the field holds.

  Returns:
    Its text form, or None.
  """
  return None if value is None else str(value)


def history_for_track(connection: sqlite3.Connection,
                      track_id: int) -> list[sqlite3.Row]:
  """Returns every recorded change for a track, oldest first.

  Args:
    connection: An open connection.
    track_id: The track's row id.

  Returns:
    Rows with batch, field, old_value, new_value and changed_at.
  """
  return list(
      connection.execute(
          "SELECT batch, field, old_value, new_value, changed_at"
          " FROM tag_history WHERE track_id = ?"
          " ORDER BY changed_at, id", (track_id,)).fetchall())


def batches_for_track(connection: sqlite3.Connection,
                      track_id: int) -> list[tuple[str, str, int]]:
  """Summarises a track's history as one entry per write.

  Args:
    connection: An open connection.
    track_id: The track's row id.

  Returns:
    (batch, when, number of fields changed), newest first.
  """
  rows = connection.execute(
      "SELECT batch, min(changed_at) AS when_, count(*) AS n"
      " FROM tag_history WHERE track_id = ?"
      " GROUP BY batch ORDER BY when_ DESC, batch DESC",
      (track_id,)).fetchall()
  return [(str(row["batch"]), str(row["when_"]), int(row["n"])) for row in rows]


def values_to_restore(connection: sqlite3.Connection, track_id: int,
                      batch: str) -> dict[str, str | None]:
  """Finds the values a track held before a given write.

  For each field touched at or after that write, the value to restore is
  the *oldest* recorded `old_value` from that point on — undoing three
  successive edits to a title must go back to what it was before the
  first, not the second.

  Args:
    connection: An open connection.
    track_id: The track's row id.
    batch: The write to roll back to, inclusive.

  Returns:
    Field name to the value it should be restored to. None means the
    field was previously unset.
  """
  rows = connection.execute(
      "SELECT field, old_value FROM tag_history"
      " WHERE track_id = ? AND changed_at >= ("
      "   SELECT min(changed_at) FROM tag_history"
      "   WHERE track_id = ? AND batch = ?)"
      " ORDER BY changed_at DESC, id DESC",
      (track_id, track_id, batch)).fetchall()
  restore: dict[str, str | None] = {}
  for row in rows:
    restore[str(row["field"])] = row["old_value"]
  return restore


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
