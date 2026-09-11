"""Persisting arbitration, and never overwriting a human (SPEC.md §15)."""

import pytest

from music import db
from music.arbitrate import Decision, load_precedence, persist


@pytest.fixture
def conn(tmp_path):
  c = db.connect(tmp_path / "arb.db")
  db.migrate(c)
  c.execute(
    "INSERT INTO source_file (id, origin, staging_path, sha256, duration_s)"
    " VALUES (1, 'local', '/tmp/a.m4a', 'x', 1.0)"
  )
  c.execute("INSERT INTO track (id, source_file_id) VALUES (1, 1)")
  return c


def test_persist_writes_decisions(conn):
  written = persist(conn, 1, [Decision("genre", "House", "discogs")])
  assert written == 1
  row = conn.execute(
    "SELECT * FROM resolved_field WHERE track_id=1 AND field='genre'"
  ).fetchone()
  assert (row["value"], row["source"], row["decided_by"]) == (
    "House",
    "discogs",
    "precedence",
  )


def test_rerunning_updates_a_precedence_decision(conn):
  persist(conn, 1, [Decision("genre", "House", "discogs")])
  persist(conn, 1, [Decision("genre", "Techno", "beatport")])
  row = conn.execute(
    "SELECT value, source FROM resolved_field WHERE track_id=1"
  ).fetchone()
  assert (row["value"], row["source"]) == ("Techno", "beatport")


def test_manual_edits_are_never_overwritten(conn):
  """Re-running the resolver must always be safe (SPEC.md §15)."""
  conn.execute(
    "INSERT INTO resolved_field (track_id, field, value, source, decided_by)"
    " VALUES (1, 'genre', 'Melodic Techno', 'me', 'manual')"
  )
  written = persist(conn, 1, [Decision("genre", "House", "discogs")])
  assert written == 0
  row = conn.execute(
    "SELECT value, decided_by FROM resolved_field WHERE track_id=1"
  ).fetchone()
  assert (row["value"], row["decided_by"]) == ("Melodic Techno", "manual")


def test_manual_edit_blocks_only_its_own_field(conn):
  conn.execute(
    "INSERT INTO resolved_field (track_id, field, value, source, decided_by)"
    " VALUES (1, 'genre', 'Mine', 'me', 'manual')"
  )
  written = persist(
    conn, 1, [Decision("genre", "House", "discogs"), Decision("label", "X", "discogs")]
  )
  assert written == 1
  fields = {
    r["field"]: r["value"]
    for r in conn.execute("SELECT field, value FROM resolved_field WHERE track_id=1")
  }
  assert fields == {"genre": "Mine", "label": "X"}


def test_calibrated_table_overrides_the_defaults(conn):
  """The elicitation exercise populates `precedence` (SPEC.md §9)."""
  assert load_precedence(conn, "pop", "genre")[0] == "discogs"
  conn.execute(
    "INSERT INTO precedence (genre_family, field, rank, source)"
    " VALUES ('pop', 'genre', 1, 'itunes')"
  )
  assert load_precedence(conn, "pop", "genre") == ("itunes",)


def test_defaults_apply_when_uncalibrated(conn):
  assert load_precedence(conn, "electronic", "genre")[0] == "beatport"
