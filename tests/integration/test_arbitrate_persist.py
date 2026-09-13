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
  assert load_precedence(conn, "pop", "genre")[0] == "itunes"
  conn.execute(
    "INSERT INTO precedence (genre_family, field, rank, source)"
    " VALUES ('pop', 'genre', 1, 'discogs')"
  )
  assert load_precedence(conn, "pop", "genre") == ("discogs",)


def test_defaults_apply_when_uncalibrated(conn):
  assert load_precedence(conn, "electronic", "genre")[0] == "itunes"
  assert load_precedence(conn, "electronic", "title")[0] == "beatport"


# --- a pasted link outranks arbitration ------------------------------------


def test_persist_never_overwrites_a_url_override(conn):
  """Pressing Re-tag used to run arbitration straight over a pasted link.

  Only `manual` was protected, so every field the link had written was
  replaced by the resolver's answer and the track visibly reverted — which is
  the one thing a link is pasted to prevent.
  """
  conn.execute(
    "INSERT INTO resolved_field (track_id, field, value, source, decided_by)"
    " VALUES (1,'album','Nothing but the Beat (Ultimate Edition)','spotify',"
    " 'url_override')"
  )
  persist(
    conn,
    1,
    [Decision("album", "Nothing But the Beat", "musicbrainz", "precedence")],
  )
  row = conn.execute(
    "SELECT value, decided_by FROM resolved_field WHERE track_id=1 AND field='album'"
  ).fetchone()
  assert row["value"] == "Nothing but the Beat (Ultimate Edition)"
  assert row["decided_by"] == "url_override"


def test_rearbitrate_does_not_delete_a_url_override(conn):
  """`rearbitrate` cleared everything that was not `manual`, links included."""
  from music.arbitrate import rearbitrate

  conn.execute(
    "INSERT INTO source_file (id, origin, staging_path, sha256, duration_s)"
    " VALUES (9,'youtube','s','h',1.0)"
  )
  conn.execute(
    "INSERT INTO track (id, source_file_id, genre_family) VALUES (9,9,'pop')"
  )
  conn.execute(
    "INSERT INTO field_candidate (track_id, field, value, source)"
    " VALUES (9,'album','Nothing But the Beat','musicbrainz')"
  )
  conn.execute(
    "INSERT INTO resolved_field (track_id, field, value, source, decided_by)"
    " VALUES (9,'album','Nothing but the Beat (Ultimate Edition)','spotify',"
    " 'url_override')"
  )
  rearbitrate(conn, 9)
  row = conn.execute(
    "SELECT value, decided_by FROM resolved_field WHERE track_id=9 AND field='album'"
  ).fetchone()
  assert row["value"] == "Nothing but the Beat (Ultimate Edition)"
  assert row["decided_by"] == "url_override"


def test_a_manual_edit_still_beats_a_link(conn):
  """Both are the user; the more recent deliberate act is the one on screen."""
  conn.execute(
    "INSERT INTO resolved_field (track_id, field, value, source, decided_by)"
    " VALUES (1,'title','Typed By Hand','manual','manual')"
  )
  persist(conn, 1, [Decision("title", "From Arbitration", "spotify", "precedence")])
  row = conn.execute(
    "SELECT value FROM resolved_field WHERE track_id=1 AND field='title'"
  ).fetchone()
  assert row["value"] == "Typed By Hand"
