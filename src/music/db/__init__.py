"""The only module that touches SQLite.

Every read and write goes through here, so adding a `user_id` later is a
single-module migration rather than a rewrite (SPEC.md §13).
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = Path(__file__).parent / "schema.sql"

# bumped whenever schema.sql changes in a way that needs a migration step.
SCHEMA_VERSION = 2


def connect(path: Path) -> sqlite3.Connection:
  """Open a connection with the pragmas this project relies on.

  Args:
    path: Database file. Parent directories are created if needed.

  Returns:
    An open connection with row access by name.
  """
  path.parent.mkdir(parents=True, exist_ok=True)
  conn = sqlite3.connect(path, isolation_level=None)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  # wal lets the web ui read while the pipeline writes (SPEC.md §15).
  conn.execute("PRAGMA journal_mode = WAL")
  conn.execute("PRAGMA synchronous = NORMAL")
  return conn


def migrate(conn: sqlite3.Connection) -> int:
  """Apply the schema. Idempotent.

  Args:
    conn: An open connection.

  Returns:
    The schema version now in force.
  """
  current = int(conn.execute("PRAGMA user_version").fetchone()[0])
  if current >= SCHEMA_VERSION:
    return current
  conn.executescript(SCHEMA.read_text(encoding="utf-8"))
  if 0 < current < 2:
    _migrate_v1_to_v2(conn)
  conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
  return SCHEMA_VERSION


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
  """Widen `resolved_field.decided_by` to allow 'fallback'.

  SQLite cannot alter a CHECK constraint, so the table is rebuilt. Rows are
  preserved.
  """
  conn.executescript("""
    CREATE TABLE resolved_field_v2 (
      track_id   INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE,
      field      TEXT    NOT NULL,
      value      TEXT,
      source     TEXT,
      decided_by TEXT    NOT NULL CHECK (
        decided_by IN ('precedence','fallback','manual','url_override')
      ),
      PRIMARY KEY (track_id, field)
    );
    INSERT INTO resolved_field_v2 SELECT * FROM resolved_field;
    DROP TABLE resolved_field;
    ALTER TABLE resolved_field_v2 RENAME TO resolved_field;
  """)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
  """Run a block in a transaction, rolling back on error.

  Args:
    conn: An open connection.

  Yields:
    The same connection.
  """
  conn.execute("BEGIN")
  try:
    yield conn
  except Exception:
    conn.execute("ROLLBACK")
    raise
  conn.execute("COMMIT")


def table_names(conn: sqlite3.Connection) -> list[str]:
  """List user tables, for verification and diagnostics.

  Args:
    conn: An open connection.

  Returns:
    Sorted table names.
  """
  rows = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
  ).fetchall()
  return sorted(r["name"] for r in rows)
