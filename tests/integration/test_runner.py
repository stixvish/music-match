"""Stage runner: ordering, resumability, failure isolation (tasks/todo.md t3)."""

import pytest

from music import db
from music.db import runner


@pytest.fixture
def conn(tmp_path):
  connection = db.connect(tmp_path / "t.db")
  db.migrate(connection)
  return connection


def _seed(conn, count):
  for i in range(count):
    conn.execute(
      "INSERT INTO source_file (id, origin, staging_path, sha256, duration_s)"
      " VALUES (?, 'youtube', ?, ?, 1.0)",
      (i + 1, f"/tmp/{i}.m4a", f"sha{i}"),
    )
    conn.execute(
      "INSERT INTO track (id, source_file_id, stage) VALUES (?, ?, 'acquired')",
      (i + 1, i + 1),
    )


def test_next_stage_order():
  assert runner.next_stage("acquired") == "normalised"
  assert runner.next_stage("published") is None
  with pytest.raises(ValueError):
    runner.next_stage("nope")


def test_advance_moves_tracks_forward(conn):
  _seed(conn, 3)
  report = runner.advance(conn, "acquired", lambda c, r: None)
  assert report.advanced == 3
  stages = [r["stage"] for r in conn.execute("SELECT stage FROM track")]
  assert stages == ["normalised"] * 3


def test_failure_is_isolated_not_fatal(conn):
  _seed(conn, 3)

  def boom(c, row):
    if row["id"] == 2:
      raise RuntimeError("bad track")

  report = runner.advance(conn, "acquired", boom)
  assert (report.advanced, report.failed) == (2, 1)
  rows = {r["id"]: r for r in conn.execute("SELECT * FROM track")}
  assert rows[2]["status"] == "failed"
  assert "bad track" in rows[2]["error"]
  # the other two still advanced
  assert rows[1]["stage"] == "normalised"
  assert rows[3]["stage"] == "normalised"


def test_run_resumes_where_it_stopped(conn):
  """Kill mid-run, restart, and pick up at the same track (tasks/todo.md t3)."""
  _seed(conn, 5)
  processed = []

  def crash_after_three(c, row):
    if len(processed) == 3:
      raise KeyboardInterrupt
    processed.append(row["id"])

  with pytest.raises(KeyboardInterrupt):
    runner.advance(conn, "acquired", crash_after_three)

  assert processed == [1, 2, 3]
  # restart: only the untouched tracks remain at the old stage
  remaining = [r["id"] for r in runner.pending(conn, "acquired")]
  assert remaining == [4, 5]

  resumed = []
  runner.advance(conn, "acquired", lambda c, r: resumed.append(r["id"]))
  assert resumed == [4, 5]


def test_failed_tracks_are_not_retried(conn):
  _seed(conn, 2)
  conn.execute("UPDATE track SET status='failed' WHERE id=1")
  assert [r["id"] for r in runner.pending(conn, "acquired")] == [2]


def test_run_all_skips_stages_without_handlers(conn):
  _seed(conn, 1)
  reports = runner.run_all(conn, {"acquired": lambda c, r: None})
  assert set(reports) == {"acquired"}
  assert conn.execute("SELECT stage FROM track").fetchone()["stage"] == "normalised"
