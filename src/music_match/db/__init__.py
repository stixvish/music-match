"""SQLite state: schema, connections, and queries."""

from music_match.db.connection import DEFAULT_DB_FILE
from music_match.db.connection import connect
from music_match.db.connection import initialize
from music_match.db.schema import SCHEMA_STATEMENTS
from music_match.db.schema import SCHEMA_VERSION
from music_match.db.schema import TABLES

__all__ = [
    "DEFAULT_DB_FILE",
    "SCHEMA_STATEMENTS",
    "SCHEMA_VERSION",
    "TABLES",
    "connect",
    "initialize",
]
