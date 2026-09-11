"""Acquisition: probe, registration, dedup (tasks/todo.md t4, t13, t16).

These shipped in phase 0 as skeleton code with no tests. Written now.
"""

import sqlite3

import pytest

from music import db
from music.acquire import (
  Download,
  already_have,
  classify_download,
  flag_video_rip,
  probe,
  register,
  register_local,
)


@pytest.fixture
def conn(tmp_path):
  c = db.connect(tmp_path / "a.db")
  db.migrate(c)
  return c


def test_probe_reads_stream_not_container(synth_audio):
  """The container bitrate includes cover art and overstates quality (§4)."""
  info = probe.probe(synth_audio)
  assert info.codec == "aac"
  assert info.sample_rate == 44100
  assert info.channels == 2
  assert 1.5 < info.duration_s < 2.5
  assert info.bitrate_kbps > 0


def test_probe_rejects_a_non_audio_file(tmp_path):
  bad = tmp_path / "not-audio.m4a"
  bad.write_bytes(b"definitely not audio")
  with pytest.raises(RuntimeError):
    probe.probe(bad)


def test_sha256_is_stable_and_content_addressed(synth_audio, tmp_path):
  copy = tmp_path / "copy.m4a"
  copy.write_bytes(synth_audio.read_bytes())
  assert probe.sha256(synth_audio) == probe.sha256(copy)


def test_register_local_creates_source_and_track(conn, synth_audio):
  track_id = register_local(conn, synth_audio)
  row = conn.execute(
    "SELECT s.* FROM source_file s JOIN track t ON t.source_file_id = s.id"
    " WHERE t.id = ?",
    (track_id,),
  ).fetchone()
  assert row["origin"] == "local"
  assert row["codec"] == "aac"
  assert row["sha256"]


def test_dedup_check_runs_before_download(conn, synth_audio):
  """Re-running a playlist must not re-download what we already own (§8)."""
  assert not already_have(conn, "abc123")
  register(
    conn,
    Download(
      video_id="abc123",
      path=synth_audio,
      title="T",
      channel="C",
      description="",
      duration_s=2.0,
      abr=256.0,
      itag="141",
    ),
  )
  assert already_have(conn, "abc123")


def test_video_id_is_unique(conn, synth_audio):
  item = Download(
    video_id="dup",
    path=synth_audio,
    title="T",
    channel="C",
    description="",
    duration_s=2.0,
    abr=256.0,
    itag="141",
  )
  register(conn, item)
  with pytest.raises(sqlite3.IntegrityError):
    register(conn, item)


def test_art_track_download_is_not_flagged(conn, synth_audio):
  item = Download(
    video_id="art",
    path=synth_audio,
    title="Make It",
    channel="Burnie - Topic",
    description="Provided to YouTube by Amp Suite",
    duration_s=2.0,
    abr=256.0,
    itag="141",
  )
  track_id = register(conn, item)
  result = classify_download(item)
  flag_video_rip(conn, track_id, result)
  row = conn.execute(
    "SELECT is_video_rip FROM track WHERE id=?", (track_id,)
  ).fetchone()
  assert row["is_video_rip"] == 0
  assert conn.execute("SELECT count(*) c FROM review_queue").fetchone()["c"] == 0


def test_video_rip_is_flagged_and_queued(conn, synth_audio):
  item = Download(
    video_id="rip",
    path=synth_audio,
    title="Nina Sky - Move Ya Body (Official Music Video)",
    channel="NINA SKY",
    description="",
    duration_s=2.0,
    abr=256.0,
    itag="141",
  )
  track_id = register(conn, item)
  flag_video_rip(conn, track_id, classify_download(item))
  assert (
    conn.execute("SELECT is_video_rip FROM track WHERE id=?", (track_id,)).fetchone()[
      "is_video_rip"
    ]
    == 1
  )
  assert (
    conn.execute(
      "SELECT reason FROM review_queue WHERE track_id=?", (track_id,)
    ).fetchone()["reason"]
    == "video_rip"
  )


def test_is_art_track_property():
  base = dict(video_id="x", path=None, title="T", duration_s=1.0, abr=256.0, itag="141")
  assert Download(**base, channel="Artist - Topic", description="").is_art_track
  assert Download(
    **base, channel="Some Channel", description="Provided to YouTube by X"
  ).is_art_track
  assert not Download(**base, channel="Some Channel", description="").is_art_track
