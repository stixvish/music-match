"""Tests for the SQLite schema and connection helpers."""

import pathlib
import sqlite3

import pytest

from music_match.db import connection
from music_match.db import schema


@pytest.fixture(name="conn")
def fixture_conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
  """Opens an initialized database in a temporary directory.

  Args:
    tmp_path: pytest's per-test temporary directory.

  Returns:
    An open, initialized connection.
  """
  db = connection.connect(tmp_path / "state" / "music_match.db")
  connection.initialize(db)
  return db


def test_initialize_creates_every_table(conn: sqlite3.Connection) -> None:
  """All four tables from the design exist after initialize."""
  assert set(connection.table_names(conn)) == set(schema.TABLES)
  assert connection.schema_version(conn) == schema.SCHEMA_VERSION


def test_initialize_creates_parent_directory(tmp_path: pathlib.Path) -> None:
  """connect makes the database's parent directory if it is missing."""
  db_path = tmp_path / "a" / "b" / "music_match.db"
  connection.connect(db_path).close()
  assert db_path.exists()


def test_initialize_is_idempotent(tmp_path: pathlib.Path) -> None:
  """Re-initializing an existing database preserves its rows."""
  db_path = tmp_path / "music_match.db"
  with connection.open_db(db_path) as first:
    first.execute("INSERT INTO tracks (path, source_name) VALUES (?, ?)",
                  ("/music/yt-dlp/a.m4a", "yt-dlp"))
    first.commit()
  with connection.open_db(db_path) as second:
    count = second.execute("SELECT count(*) AS n FROM tracks").fetchone()["n"]
  assert count == 1


def test_track_path_is_unique(conn: sqlite3.Connection) -> None:
  """The same file cannot be indexed twice."""
  conn.execute("INSERT INTO tracks (path, source_name) VALUES (?, ?)",
               ("/music/yt-dlp/a.m4a", "yt-dlp"))
  with pytest.raises(sqlite3.IntegrityError):
    conn.execute("INSERT INTO tracks (path, source_name) VALUES (?, ?)",
                 ("/music/yt-dlp/a.m4a", "yt-dlp"))


def test_match_status_is_constrained(conn: sqlite3.Connection) -> None:
  """A typo'd match status is rejected rather than silently stored."""
  with pytest.raises(sqlite3.IntegrityError):
    conn.execute(
        "INSERT INTO tracks (path, source_name, match_status)"
        " VALUES (?, ?, ?)", ("/music/yt-dlp/a.m4a", "yt-dlp", "maybe"))


def test_new_track_defaults_to_pending(conn: sqlite3.Connection) -> None:
  """A freshly indexed track starts in the pending state."""
  conn.execute("INSERT INTO tracks (path, source_name) VALUES (?, ?)",
               ("/music/yt-dlp/a.m4a", "yt-dlp"))
  row = conn.execute(
      "SELECT match_status, first_seen_at FROM tracks").fetchone()
  assert row["match_status"] == "pending"
  assert row["first_seen_at"]


def insert_track(conn: sqlite3.Connection,
                 path: str = "/m/yt-dlp/a.m4a") -> int:
  """Inserts a track and returns its id.

  Args:
    conn: An open connection.
    path: The track's path.

  Returns:
    The new row id.
  """
  cursor = conn.execute("INSERT INTO tracks (path, source_name) VALUES (?, ?)",
                        (path, "yt-dlp"))
  return int(cursor.lastrowid or 0)


def test_tag_history_cascades_on_track_delete(conn: sqlite3.Connection) -> None:
  """Foreign keys are enforced, so history dies with its track."""
  track_id = insert_track(conn)
  conn.execute(
      "INSERT INTO tag_history (track_id, batch, field, old_value, new_value)"
      " VALUES (?, ?, ?, ?, ?)",
      (track_id, "batch-1", "genre", "Music", "Deep House"))
  conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
  remaining = conn.execute("SELECT count(*) AS n FROM tag_history").fetchone()
  assert remaining["n"] == 0


def test_tag_history_rejects_unknown_track(conn: sqlite3.Connection) -> None:
  """History rows cannot reference a track that does not exist."""
  with pytest.raises(sqlite3.IntegrityError):
    conn.execute(
        "INSERT INTO tag_history (track_id, batch, field, new_value)"
        " VALUES (?, ?, ?, ?)", (999, "batch-1", "genre", "Deep House"))


def test_album_art_history_stores_a_hash(conn: sqlite3.Connection) -> None:
  """Art history holds content hashes, not image bytes."""
  track_id = insert_track(conn)
  digest = "a" * 64
  conn.execute(
      "INSERT INTO tag_history (track_id, batch, field, old_value, new_value)"
      " VALUES (?, ?, ?, ?, ?)",
      (track_id, "batch-1", "album_art", None, digest))
  row = conn.execute(
      "SELECT new_value FROM tag_history WHERE field = 'album_art'").fetchone()
  assert row["new_value"] == digest


def test_download_archive_is_keyed_per_extractor(
    conn: sqlite3.Connection) -> None:
  """The same id from two extractors is two entries, not a collision."""
  conn.execute(
      "INSERT INTO download_archive (extractor, video_id) VALUES (?, ?)",
      ("youtube", "abc123"))
  conn.execute(
      "INSERT INTO download_archive (extractor, video_id) VALUES (?, ?)",
      ("soundcloud", "abc123"))
  with pytest.raises(sqlite3.IntegrityError):
    conn.execute(
        "INSERT INTO download_archive (extractor, video_id) VALUES (?, ?)",
        ("youtube", "abc123"))


def test_archive_entry_survives_track_deletion(
    conn: sqlite3.Connection) -> None:
  """Deleting a track must not make its video ID downloadable again."""
  track_id = insert_track(conn)
  conn.execute(
      "INSERT INTO download_archive (video_id, track_id) VALUES (?, ?)",
      ("abc123", track_id))
  conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
  row = conn.execute(
      "SELECT video_id, track_id FROM download_archive").fetchone()
  assert row["video_id"] == "abc123"
  assert row["track_id"] is None


def test_wont_match_is_one_row_per_track(conn: sqlite3.Connection) -> None:
  """A track can only be marked unmatched once."""
  track_id = insert_track(conn)
  conn.execute("INSERT INTO wont_match (track_id, reason) VALUES (?, ?)",
               (track_id, "self-made edit"))
  with pytest.raises(sqlite3.IntegrityError):
    conn.execute("INSERT INTO wont_match (track_id) VALUES (?)", (track_id,))


def test_migration_adds_genre_confidence_to_an_older_database(
    tmp_path: pathlib.Path) -> None:
  """A version 1 database gains the column without losing its rows.

  The database is gitignored and rebuildable, but a confusing crash on
  reopening one is worth a migration to avoid.
  """
  db_path = tmp_path / "old.db"
  old = connection.connect(db_path)
  old.execute("CREATE TABLE tracks (id INTEGER PRIMARY KEY,"
              " path TEXT NOT NULL UNIQUE, source_name TEXT NOT NULL,"
              " fingerprint TEXT, duration_seconds REAL, detected_genre TEXT,"
              " source_video_id TEXT, tags_json TEXT,"
              " match_status TEXT NOT NULL DEFAULT 'pending',"
              " first_seen_at TEXT, updated_at TEXT)")
  old.execute("INSERT INTO tracks (path, source_name) VALUES (?, ?)",
              ("/music/yt-dlp/a.m4a", "yt-dlp"))
  old.execute("PRAGMA user_version = 1")
  old.commit()
  old.close()

  conn = connection.connect(db_path)
  connection.initialize(conn)
  columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
  assert "genre_confidence" in columns
  assert connection.schema_version(conn) == schema.SCHEMA_VERSION
  assert conn.execute("SELECT count(*) AS n FROM tracks").fetchone()["n"] == 1


def test_migrating_twice_is_harmless(tmp_path: pathlib.Path) -> None:
  """Re-initializing an already-migrated database does not fail."""
  db_path = tmp_path / "state.db"
  with connection.open_db(db_path) as conn:
    connection.initialize(conn)
    assert connection.schema_version(conn) == schema.SCHEMA_VERSION


def test_fresh_database_skips_migrations(tmp_path: pathlib.Path) -> None:
  """A new database is created with every column, nothing to migrate."""
  with connection.open_db(tmp_path / "new.db") as conn:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
  assert "genre_confidence" in columns
