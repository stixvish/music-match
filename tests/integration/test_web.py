"""Web API (tasks/todo.md t28-t30, SPEC.md §15)."""

import pytest
from fastapi.testclient import TestClient

from music import db
from music.web import create_app


@pytest.fixture
def client(tmp_path, synth_audio):
  path = tmp_path / "w.db"
  conn = db.connect(path)
  db.migrate(conn)
  conn.execute(
    "INSERT INTO source_file (id, origin, staging_path, sha256, duration_s, channel)"
    " VALUES (1, 'youtube', ?, 'x', 2.0, 'Artist - Topic')",
    (str(synth_audio),),
  )
  conn.execute(
    "INSERT INTO track (id, source_file_id, status, genre_family,"
    " identity_confidence) VALUES (1, 1, 'review', 'pop', 0.5)"
  )
  conn.execute(
    "INSERT INTO review_queue (track_id, reason) VALUES (1, 'low_confidence')"
  )
  for field, value, source, by in [
    ("title", "A Song", "musicbrainz", "precedence"),
    ("artist", "An Artist", "musicbrainz", "precedence"),
    ("genre", "House", "discogs", "precedence"),
  ]:
    conn.execute(
      "INSERT INTO resolved_field (track_id, field, value, source, decided_by)"
      " VALUES (1,?,?,?,?)",
      (field, value, source, by),
    )
  for field, value, source in [
    ("genre", "House", "discogs"),
    ("genre", "Dance", "itunes"),
    ("genre", "Electronic", "essentia"),
  ]:
    conn.execute(
      "INSERT INTO field_candidate (track_id, field, value, source) VALUES (1,?,?,?)",
      (field, value, source),
    )
  conn.close()
  return TestClient(create_app(database=path))


def test_index_and_assets_load(client):
  assert client.get("/").status_code == 200
  assert "review" in client.get("/").text.lower()
  assert client.get("/app.css").status_code == 200
  assert client.get("/app.js").status_code == 200


def test_queue_lists_review_items(client):
  rows = client.get("/api/tracks?status=review").json()
  assert len(rows) == 1
  assert rows[0]["reason"] == "low_confidence"
  assert rows[0]["title"] == "A Song"


def test_track_returns_resolved_and_candidates(client):
  data = client.get("/api/track/1").json()
  assert data["track"]["genre_family"] == "pop"
  assert {r["field"] for r in data["resolved"]} == {"title", "artist", "genre"}
  genres = [c["source"] for c in data["candidates"] if c["field"] == "genre"]
  assert set(genres) == {"discogs", "itunes", "essentia"}


def test_missing_track_is_404(client):
  assert client.get("/api/track/999").status_code == 404


def test_editing_a_field_marks_it_manual(client):
  """A human decided this; no resolver run may overrule it (SPEC.md §15)."""
  client.post("/api/track/1/field", json={"field": "genre", "value": "Melodic Techno"})
  resolved = {r["field"]: r for r in client.get("/api/track/1").json()["resolved"]}
  assert resolved["genre"]["value"] == "Melodic Techno"
  assert resolved["genre"]["decided_by"] == "manual"


def test_accepting_clears_the_queue(client):
  assert client.get("/api/tracks?status=review").json()
  client.post("/api/track/1/accept")
  assert client.get("/api/tracks?status=review").json() == []


def test_url_override_is_parsed_and_stored(client):
  r = client.post(
    "/api/track/1/url",
    json={"url": "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"},
  )
  assert r.status_code == 200
  assert r.json()["provider"] == "spotify"
  assert r.json()["id"] == "4cOdK2wGLETKBW3PvgPWqT"


def test_unrecognised_url_is_rejected(client):
  r = client.post("/api/track/1/url", json={"url": "https://example.com/nope"})
  assert r.status_code == 400


def test_audio_is_served_for_preview(client):
  """Audio preview is non-negotiable.

  The common failure is a plausible match that is the wrong recording, and you
  have to hear it (SPEC.md §15).
  """
  r = client.get("/api/audio/1")
  assert r.status_code == 200
  assert len(r.content) > 1000
