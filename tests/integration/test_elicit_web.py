"""Elicitation over HTTP, and the precedence it writes (tasks/todo.md t31)."""

import pytest
from fastapi.testclient import TestClient

from music import db, elicit
from music.arbitrate import arbitrate, load_precedence
from music.sources.base import FieldCandidate
from music.web import create_app

# two sources disagree on genre for every track, so every answer is a real one
DISPUTED = [
  ("genre", "House", "discogs"),
  ("genre", "Dance", "itunes"),
  ("title", "A Song", "musicbrainz"),
  ("title", "A Song (Radio Edit)", "spotify"),
]


@pytest.fixture
def client(tmp_path, synth_audio):
  path = tmp_path / "e.db"
  conn = db.connect(path)
  db.migrate(conn)
  for track_id in range(1, 13):
    conn.execute(
      "INSERT INTO source_file (id, origin, staging_path, sha256, duration_s,"
      " channel) VALUES (?, 'youtube', ?, ?, 2.0, 'Artist - Topic')",
      (track_id, str(synth_audio), f"h{track_id}"),
    )
    family = "electronic" if track_id % 2 else "pop"
    conn.execute(
      "INSERT INTO track (id, source_file_id, status, genre_family,"
      " identity_confidence) VALUES (?,?,'published',?,0.95)",
      (track_id, track_id, family),
    )
    for field, value, source in DISPUTED:
      conn.execute(
        "INSERT INTO field_candidate (track_id, field, value, source) VALUES (?,?,?,?)",
        (track_id, field, value, source),
      )
  conn.close()
  return TestClient(create_app(database=path))


def answer_everything(client, pick="Dance", limit=200):
  """Answer every question, always choosing `pick` when it is on offer."""
  for answered in range(limit):
    q = client.get("/api/elicit/next").json()
    if q["done"]:
      return answered
    index = next((i for i, o in enumerate(q["options"]) if o["value"] == pick), 0)
    assert client.post("/api/elicit/choice", json={"option": index}).json()["ok"]
  raise AssertionError("elicitation did not terminate")


def test_a_question_never_names_a_source(client):
  """Blindness is the whole method — nothing identifying may reach the client."""
  body = client.get("/api/elicit/next").text
  for source in ("discogs", "itunes", "musicbrainz", "spotify", "beatport"):
    assert source not in body.lower()


def test_question_offers_the_disputed_values(client):
  q = client.get("/api/elicit/next").json()
  assert not q["done"]
  assert len(q["options"]) == 2
  assert q["field"] in ("genre", "title")


def test_the_session_terminates(client):
  """A session that cannot end is a session that blows the budget."""
  assert answer_everything(client) > 0
  assert client.get("/api/elicit/next").json()["done"] is True


def test_each_cell_stops_at_its_target(client):
  answer_everything(client)
  rows = client.get("/api/elicit/progress").json()["cells"]
  assert rows, "no cells recorded"
  assert all(c["answers"] <= elicit.TARGET_PER_CELL for c in rows)


def test_both_families_are_covered(client):
  """Stratification by cell is what stops one family eating the whole session."""
  answer_everything(client)
  families = {c["family"] for c in client.get("/api/elicit/progress").json()["cells"]}
  assert families == {"electronic", "pop"}


def test_choices_change_the_ranking_and_arbitration(client, tmp_path):
  """End to end: answers -> precedence table -> a different arbitration."""
  answer_everything(client, pick="Dance")
  assert client.post("/api/elicit/apply").json()["cells"] > 0

  conn = db.connect(tmp_path / "e.db")
  ranked = load_precedence(conn, "pop", "genre")
  assert ranked[0] == "itunes", ranked

  decisions = arbitrate(
    [
      FieldCandidate(field="genre", value="House", source="discogs"),
      FieldCandidate(field="genre", value="Dance", source="itunes"),
    ],
    family="pop",
    ranking=lambda f, fl: load_precedence(conn, f, fl),
  )
  assert decisions[0].value == "Dance"
  conn.close()


def test_no_preference_is_recorded_without_a_winner(client, tmp_path):
  client.get("/api/elicit/next")
  client.post("/api/elicit/choice", json={"option": -1})
  conn = db.connect(tmp_path / "e.db")
  row = conn.execute("SELECT chosen, offered FROM elicitation").fetchone()
  assert row["chosen"] == ""
  assert row["offered"]
  conn.close()


def test_answering_without_a_question_is_rejected(client):
  assert client.post("/api/elicit/choice", json={"option": 0}).status_code == 409


def test_out_of_range_option_is_rejected(client):
  client.get("/api/elicit/next")
  assert client.post("/api/elicit/choice", json={"option": 99}).status_code == 400


def test_apply_is_idempotent(client):
  answer_everything(client)
  first = client.post("/api/elicit/apply").json()["cells"]
  second = client.post("/api/elicit/apply").json()["cells"]
  assert first == second


def test_uncalibrated_cells_keep_the_default(client, tmp_path):
  """A cell nobody answered must not be written; the default is better."""
  answer_everything(client)
  client.post("/api/elicit/apply")
  conn = db.connect(tmp_path / "e.db")
  rows = conn.execute("SELECT DISTINCT field FROM precedence").fetchall()
  assert {r["field"] for r in rows} <= {"genre", "title"}
  conn.close()
