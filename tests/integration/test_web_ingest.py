"""Adding tracks from the web ui (SPEC.md §15)."""

import time

import pytest
from fastapi.testclient import TestClient

from music import db, pipeline
from music.web import create_app


@pytest.fixture
def client(tmp_path):
  path = tmp_path / "i.db"
  conn = db.connect(path)
  db.migrate(conn)
  conn.close()
  return TestClient(create_app(database=path))


@pytest.fixture
def fake_ingest(monkeypatch):
  """Replace the pipeline so no test ever touches the network."""
  calls: list[str] = []

  def fake(url, cfg, *, limit=0, conn=None, progress=None, **kwargs):
    calls.append(url)
    state = progress or pipeline.Progress()
    state.url = url
    state.total = 2
    state.done = 2
    state.published = 1
    state.queued = 1
    state.finished = True
    return state

  monkeypatch.setattr(pipeline, "ingest", fake)
  return calls


def wait_for_finish(client, tries=100):
  for _ in range(tries):
    status = client.get("/api/ingest/status").json()
    if status.get("url") and not status["running"]:
      return status
    time.sleep(0.02)
  raise AssertionError("ingest never finished")


def test_a_link_starts_an_ingest(client, fake_ingest):
  r = client.post("/api/ingest", json={"url": "https://youtu.be/abc"})
  assert r.json()["ok"]
  assert wait_for_finish(client)["published"] == 1
  assert fake_ingest == ["https://youtu.be/abc"]


def test_status_is_quiet_before_anything_runs(client):
  assert client.get("/api/ingest/status").json() == {"running": False}


def test_an_empty_url_is_rejected(client, fake_ingest):
  assert client.post("/api/ingest", json={"url": "  "}).status_code == 400
  assert fake_ingest == []


def test_a_failing_pipeline_reports_instead_of_crashing(client, monkeypatch):
  """A bad link is a message in the ui, not a dead worker thread."""

  def boom(*args, **kwargs):
    raise RuntimeError("unsupported url")

  monkeypatch.setattr(pipeline, "ingest", boom)
  client.post("/api/ingest", json={"url": "https://example.com/x"})
  assert "unsupported url" in wait_for_finish(client)["error"]


def test_a_second_ingest_is_refused_while_one_runs(client, monkeypatch):
  """Two concurrent runs would race on staging and double the request rate."""
  release = False

  def slow(url, cfg, *, limit=0, conn=None, progress=None, **kwargs):
    state = progress or pipeline.Progress()
    state.url = url
    while not release:
      time.sleep(0.01)
    state.finished = True
    return state

  monkeypatch.setattr(pipeline, "ingest", slow)
  client.post("/api/ingest", json={"url": "https://youtu.be/one"})
  for _ in range(100):
    if client.get("/api/ingest/status").json().get("url"):
      break
    time.sleep(0.02)
  second = client.post("/api/ingest", json={"url": "https://youtu.be/two"})
  release = True
  assert second.status_code == 409


def test_a_finished_run_does_not_block_the_next_one(client, fake_ingest):
  client.post("/api/ingest", json={"url": "https://youtu.be/one"})
  wait_for_finish(client)
  assert (
    client.post("/api/ingest", json={"url": "https://youtu.be/two"}).status_code == 200
  )
