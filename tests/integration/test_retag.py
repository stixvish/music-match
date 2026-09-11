"""Re-tagging from the database (tasks/todo.md t27, SPEC.md §15).

The payoff for database-as-source-of-truth: a better resolver re-tags the
library without re-downloading anything, and never overwrites a human.
"""

import pytest

from music import db
from music.arbitrate import Decision, persist
from music.publish import fields, layout_path, retag, tag, transcode


@pytest.fixture
def published(tmp_path, synth_audio):
  """A published track with a database row pointing at it."""
  conn = db.connect(tmp_path / "r.db")
  db.migrate(conn)
  library = tmp_path / "library"
  dest = layout_path(library, "pop", "Pitbull", "Time of Our Lives")
  transcode.to_aiff(synth_audio, dest)

  conn.execute(
    "INSERT INTO source_file (id, origin, staging_path, sha256, duration_s)"
    " VALUES (1, 'local', ?, 'x', 2.0)",
    (str(synth_audio),),
  )
  conn.execute(
    "INSERT INTO track (id, source_file_id, genre_family, published_path,"
    " status) VALUES (1, 1, 'pop', ?, 'published')",
    (str(dest),),
  )
  persist(
    conn,
    1,
    [
      Decision("title", "Time of Our Lives", "musicbrainz"),
      Decision("artist", "Pitbull", "musicbrainz"),
      Decision("album", "Globalization", "musicbrainz"),
      Decision("genre", "Dance-pop", "discogs"),
    ],
  )
  tag.write(dest, fields.build_for(conn, 1).tags)
  return conn, dest, library


def test_retag_writes_current_database_state(published):
  conn, dest, _ = published
  persist(conn, 1, [Decision("genre", "Progressive House", "beatport")])
  path = retag(conn, 1)
  assert path is not None
  assert tag.read(path)["genre"] == "Progressive House"


def test_retag_preserves_a_manual_edit(published):
  """A human decided this; a later resolver run must not overrule it."""
  conn, dest, _ = published
  conn.execute(
    "INSERT OR REPLACE INTO resolved_field"
    " (track_id, field, value, source, decided_by)"
    " VALUES (1, 'genre', 'Melodic Techno', 'me', 'manual')"
  )
  # arbitration tries to overwrite it and is refused
  persist(conn, 1, [Decision("genre", "House", "discogs")])
  path = retag(conn, 1)
  assert tag.read(path)["genre"] == "Melodic Techno"


def test_retag_renames_when_canonical_naming_changes(published):
  conn, dest, library = published
  persist(conn, 1, [Decision("title", "Time of Our Lives (Extended Mix)", "beatport")])
  path = retag(conn, 1)
  assert path is not None
  assert path.name == "Pitbull - Time of Our Lives [Extended Mix].aiff"
  assert not dest.exists()
  row = conn.execute("SELECT published_path FROM track WHERE id=1").fetchone()
  assert row["published_path"] == str(path)


def test_retag_keeps_existing_artwork(published):
  conn, dest, _ = published
  tags = tag.read(dest)
  tags.artwork = b"\xff\xd8\xff\xe0" + b"\x00" * 64
  tag.write(dest, tags)
  path = retag(conn, 1)
  assert tag.read(path).artwork == tags.artwork


def test_retag_on_an_unpublished_track_is_a_noop(published):
  conn, _, _ = published
  conn.execute("UPDATE track SET published_path = NULL WHERE id = 1")
  assert retag(conn, 1) is None


def test_retag_on_a_missing_file_is_a_noop(published):
  conn, dest, _ = published
  dest.unlink()
  assert retag(conn, 1) is None
