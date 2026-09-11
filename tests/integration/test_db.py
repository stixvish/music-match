"""Database schema and migration (tasks/todo.md t2)."""

import sqlite3

import pytest

from music import db

EXPECTED_TABLES = [
  "api_cache",
  "elicitation",
  "field_candidate",
  "precedence",
  "resolved_field",
  "review_queue",
  "source_file",
  "track",
]


@pytest.fixture
def conn(tmp_path):
  connection = db.connect(tmp_path / "test.db")
  db.migrate(connection)
  return connection


def test_migrate_creates_every_table(conn):
  assert db.table_names(conn) == EXPECTED_TABLES


def test_migrate_is_idempotent(tmp_path):
  path = tmp_path / "x.db"
  first = db.connect(path)
  assert db.migrate(first) == db.SCHEMA_VERSION
  # second run must be a no-op, not an error
  assert db.migrate(first) == db.SCHEMA_VERSION
  assert db.table_names(first) == EXPECTED_TABLES


def test_foreign_keys_are_enforced(conn):
  with pytest.raises(sqlite3.IntegrityError):
    conn.execute("INSERT INTO track (source_file_id, stage) VALUES (999, 'acquired')")


def test_status_check_constraint(conn):
  conn.execute(
    "INSERT INTO source_file (id, origin, staging_path, sha256, duration_s)"
    " VALUES (1, 'youtube', '/tmp/a.m4a', 'abc', 10.0)"
  )
  with pytest.raises(sqlite3.IntegrityError):
    conn.execute("INSERT INTO track (source_file_id, status) VALUES (1, 'nonsense')")


def test_transaction_rolls_back(conn):
  with pytest.raises(ValueError), db.transaction(conn):
    conn.execute(
      "INSERT INTO source_file (origin, staging_path, sha256, duration_s)"
      " VALUES ('local', '/tmp/b.m4a', 'def', 5.0)"
    )
    raise ValueError("boom")
  assert conn.execute("SELECT count(*) c FROM source_file").fetchone()["c"] == 0
