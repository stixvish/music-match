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

  def fake(url, cfg, *, limit=0, conn=None, progress=None, log_buffer=None, **kwargs):
    calls.append(url)
    state = progress or pipeline.Progress()
    state.url = url
    state.total = 2
    state.done = 2
    state.downloaded = 2
    state.published = 1
    state.queued = 1
    state.finished = True
    if log_buffer is not None:
      log_buffer.add("[youtube] fetching player", "info")
      log_buffer.add("  published: A Song.aiff", "step")
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


def test_the_app_hands_its_log_buffer_to_the_pipeline(client, fake_ingest):
  """The console is only live if the run writes into the buffer the ui reads.

  This wiring is a single keyword argument and its absence is invisible: the
  run still succeeds, the counters still move, and the console is simply empty
  forever. It was dropped once already.
  """
  client.post("/api/ingest", json={"url": "https://youtu.be/abc"})
  wait_for_finish(client)
  body = client.get("/api/ingest/log?since=-1").json()
  assert [line["text"] for line in body["lines"]] == [
    "[youtube] fetching player",
    "  published: A Song.aiff",
  ]
  assert body["cursor"] == 1


def test_the_log_is_fetched_incrementally(client, fake_ingest):
  """A one-second poll must not re-send the whole buffer every time."""
  client.post("/api/ingest", json={"url": "https://youtu.be/abc"})
  wait_for_finish(client)
  first = client.get("/api/ingest/log?since=-1").json()
  again = client.get(f"/api/ingest/log?since={first['cursor']}").json()
  assert again["lines"] == []


def test_stage_counters_reach_the_ui(client, fake_ingest):
  """`done` alone cannot distinguish downloading from waiting on an api."""
  client.post("/api/ingest", json={"url": "https://youtu.be/abc"})
  status = wait_for_finish(client)
  for key in ("downloaded", "analysed", "resolved", "published", "queued"):
    assert key in status, key
  assert status["downloaded"] == 2


def test_several_links_are_enumerated_together(monkeypatch, client):
  """One box, many playlists — and a track on two of them downloads once."""
  from music import config, pipeline
  from music.acquire import VideoRef

  seen: list[str] = []

  def fake_enumerate(url, cfg, sink=None):
    seen.append(url)
    return {
      "https://youtu.be/list-a": [
        VideoRef(video_id="x", title="Shared", channel="c", duration_s=1),
        VideoRef(video_id="y", title="Only A", channel="c", duration_s=1),
      ],
      "https://youtu.be/list-b": [
        VideoRef(video_id="x", title="Shared", channel="c", duration_s=1),
        VideoRef(video_id="z", title="Only B", channel="c", duration_s=1),
      ],
    }[url]

  monkeypatch.setattr(pipeline, "enumerate_playlist", fake_enumerate)
  monkeypatch.setattr(pipeline, "already_have", lambda conn, vid: True)

  state = pipeline.ingest(
    "https://youtu.be/list-a\nhttps://youtu.be/list-b",
    config.load(),
    conn=object(),
    resolver=object(),
  )
  assert seen == ["https://youtu.be/list-a", "https://youtu.be/list-b"]
  assert state.total == 3, "the shared video must be counted once"
  assert state.skipped == 3


def test_the_log_endpoint_is_idempotent_for_a_given_cursor(client, fake_ingest):
  """Two reads at the same cursor return the same lines — by design.

  This is what made the console duplicate every line. The client polled on a
  `setInterval`, which fires whether or not the previous tick returned, and
  each tick made several requests. Overlapping ticks both read the same stale
  cursor and both appended the batch, so "tagging and filing" showed up twice.

  The endpoint is right to answer this way; the client is now single-flight
  and reschedules only after a tick completes.
  """
  client.post("/api/ingest", json={"url": "https://youtu.be/abc"})
  wait_for_finish(client)
  first = client.get("/api/ingest/log?since=-1").json()
  repeat = client.get("/api/ingest/log?since=-1").json()
  assert first["lines"] == repeat["lines"]
  assert first["cursor"] == repeat["cursor"]

  after = client.get(f"/api/ingest/log?since={first['cursor']}").json()
  assert after["lines"] == []
