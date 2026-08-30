"""Opening and initializing the SQLite database."""

import contextlib
import pathlib
import sqlite3
from typing import Iterator

from music_match.db import schema

DEFAULT_DB_FILE = pathlib.Path("music_match.db")


def connect(path: pathlib.Path = DEFAULT_DB_FILE) -> sqlite3.Connection:
  """Opens a connection with the pragmas this project relies on.

  Foreign keys are off by default in SQLite and must be enabled per
  connection, so the ON DELETE CASCADE rules in the schema only hold if
  every connection goes through here.

  Args:
    path: Path to the database file. Its parent directory is created if
      needed. Pass ":memory:" as a Path for a throwaway database.

  Returns:
    An open connection with row access by column name.
  """
  if str(path) != ":memory:":
    path.parent.mkdir(parents=True, exist_ok=True)
  connection = sqlite3.connect(path)
  connection.row_factory = sqlite3.Row
  connection.execute("PRAGMA foreign_keys = ON")
  connection.execute("PRAGMA journal_mode = WAL")
  return connection


def initialize(connection: sqlite3.Connection) -> int:
  """Creates any missing tables and brings the schema up to date.

  Safe to call on an existing database, and the same call for first-time
  setup and for reopening: the CREATE statements are all IF NOT EXISTS,
  and migrations only run on a database that predates a column.

  Args:
    connection: An open connection, from `connect`.

  Returns:
    The schema version now recorded in the database.

  Raises:
    sqlite3.DatabaseError: If a migration fails, leaving the recorded
      version unchanged so the next attempt retries it.
  """
  starting = schema_version(connection)
  with connection:
    for statement in schema.SCHEMA_STATEMENTS:
      connection.execute(statement)
    # Version 0 means the tables were just created, and they were created
    # with every column already present — there is nothing to migrate.
    if starting:
      for version in range(starting, schema.SCHEMA_VERSION):
        for statement in schema.MIGRATIONS.get(version, ()):
          connection.execute(statement)
    connection.execute(f"PRAGMA user_version = {schema.SCHEMA_VERSION}")
  return schema.SCHEMA_VERSION


def schema_version(connection: sqlite3.Connection) -> int:
  """Returns the schema version recorded in the database, 0 if unset.

  Args:
    connection: An open connection.

  Returns:
    The `user_version` pragma value.
  """
  row = connection.execute("PRAGMA user_version").fetchone()
  return int(row[0])


def table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
  """Returns the names of the tables present, excluding SQLite internals.

  Args:
    connection: An open connection.

  Returns:
    Table names, alphabetically.
  """
  rows = connection.execute("SELECT name FROM sqlite_master"
                            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                            " ORDER BY name").fetchall()
  return tuple(row["name"] for row in rows)


@contextlib.contextmanager
def open_db(
    path: pathlib.Path = DEFAULT_DB_FILE) -> Iterator[sqlite3.Connection]:
  """Opens an initialized database and closes it on exit.

  Args:
    path: Path to the database file.

  Yields:
    An open, initialized connection.
  """
  connection = connect(path)
  try:
    initialize(connection)
    yield connection
  finally:
    connection.close()
