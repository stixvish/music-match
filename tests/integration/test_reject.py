"""Deleting a track you will never play (SPEC.md §14)."""

import pytest

from music import db, publish
from music.acquire import already_have


@pytest.fixture
def published(tmp_path, synth_audio):
  """A published track, with its staging download still present."""
  conn = db.connect(tmp_path / "r.db")
  db.migrate(conn)
  staging = tmp_path / "staging"
  staging.mkdir()
  audio = staging / "vid123.m4a"
  audio.write_bytes(synth_audio.read_bytes())

  conn.execute(
    "INSERT INTO source_file (id, origin, video_id, staging_path, sha256,"
    " duration_s) VALUES (1,'youtube','vid123',?, 'h', 2.0)",
    (str(audio),),
  )
  conn.execute(
    "INSERT INTO track (id, source_file_id, status, genre_family)"
    " VALUES (1,1,'review','pop')"
  )
  conn.execute(
    "INSERT INTO review_queue (track_id, reason) VALUES (1,'low_confidence')"
  )
  for field, value in (("artist", "An Artist"), ("title", "A Song")):
    conn.execute(
      "INSERT INTO resolved_field (track_id, field, value, source, decided_by)"
      " VALUES (1,?,?, 'musicbrainz','precedence')",
      (field, value),
    )
  library = tmp_path / "library"
  path = publish.publish_track(conn, 1, library, tmp_path / "work")
  return conn, path, audio, library


def test_both_copies_of_the_audio_go(published):
  """Reclaiming the space is the point, so the download goes too."""
  conn, path, staging, library = published
  assert path.exists() and staging.exists()

  result = publish.reject(conn, 1, library)

  assert not path.exists(), "the published file survived"
  assert not staging.exists(), "the staging download survived"
  assert set(result["removed"]) == {"published", "staging"}
  assert result["freed_bytes"] > 0


def test_the_track_is_never_downloaded_again(published):
  """The whole point when a hundred arrive and a dozen are unplayable.

  `already_have` is checked before downloading, so keeping the `source_file`
  row is what makes the rejection stick across a re-ingest of the playlist.
  """
  conn, _path, _staging, library = published
  publish.reject(conn, 1, library)
  assert already_have(conn, "vid123"), "a re-ingest would download it again"


def test_it_leaves_the_review_queue(published):
  conn, _p, _s, library = published
  assert conn.execute("SELECT count(*) FROM review_queue").fetchone()[0] == 1
  publish.reject(conn, 1, library)
  assert conn.execute("SELECT count(*) FROM review_queue").fetchone()[0] == 0


def test_the_decision_is_visible_afterwards(published):
  """A rejection should read as a decision, not as a track that vanished."""
  conn, _p, _s, library = published
  publish.reject(conn, 1, library)
  row = conn.execute("SELECT status, published_path FROM track WHERE id=1").fetchone()
  assert row["status"] == "skipped"
  assert row["published_path"] is None


def test_the_label_names_the_track(published):
  conn, _p, _s, library = published
  assert publish.reject(conn, 1, library)["label"] == "An Artist - A Song"


def test_rejecting_an_unknown_track_is_an_error(published):
  conn, _p, _s, library = published
  with pytest.raises(RuntimeError):
    publish.reject(conn, 999, library)


def test_rejecting_twice_is_harmless(published):
  """A double click must not raise; the files are simply already gone."""
  conn, _p, _s, library = published
  publish.reject(conn, 1, library)
  again = publish.reject(conn, 1, library)
  assert again["removed"] == []
  assert again["freed_bytes"] == 0
