"""Beatport parsing and token lifecycle (tasks/todo.md, SPEC.md §7).

The payloads below follow Beatport's documented schema. They are **not**
recorded from a live response — no credentials have been issued — so they
verify our handling, not Beatport's actual output. Re-record them against the
real API before trusting the field mapping.
"""

import time

import pytest

from music.sources.beatport import (
  REFRESH_MARGIN_S,
  Beatport,
  BeatportCredentialsError,
  Token,
  parse_track,
)

TRACK = {
  "name": "Strobe",
  "mix_name": "Original Mix",
  "artists": [{"name": "deadmau5"}],
  "remixers": [],
  "genre": {"name": "Dance / Electro Pop"},
  "sub_genre": {"name": "Progressive House"},
  "release": {
    "name": "For Lack of a Better Name",
    "label": {"name": "mau5trap"},
    "catalog_number": "MAU5001",
  },
  "publish_date": "2009-09-22",
  "isrc": "USUS10901234",
  "bpm": 128,
  "key": {"name": "B Minor", "camelot_number": 10, "camelot_letter": "A"},
}


def test_sub_genre_beats_top_level_genre():
  """Top-level genre is the coarse label purchased files carry (SPEC.md §7)."""
  got = {c.field: c.value for c in parse_track(TRACK)}
  assert got["genre"] == "Progressive House"


def test_falls_back_to_top_level_genre():
  track = dict(TRACK)
  track["sub_genre"] = {}
  got = {c.field: c.value for c in parse_track(track)}
  assert got["genre"] == "Dance / Electro Pop"


def test_core_fields():
  got = {c.field: c.value for c in parse_track(TRACK)}
  assert got["title"] == "Strobe"
  assert got["artist"] == "deadmau5"
  assert got["mix_name"] == "Original Mix"
  assert got["album"] == "For Lack of a Better Name"
  assert got["label"] == "mau5trap"
  assert got["catalog_number"] == "MAU5001"
  assert got["isrc"] == "USUS10901234"
  assert got["bpm"] == "128"
  assert got["release_date"] == "2009-09-22"
  assert got["year"] == "2009"


def test_key_is_rendered_in_camelot():
  got = {c.field: c.value for c in parse_track(TRACK)}
  assert got["key"] == "10A"


def test_key_falls_back_to_name_without_camelot():
  track = dict(TRACK)
  track["key"] = {"name": "B Minor"}
  assert {c.field: c.value for c in parse_track(track)}["key"] == "B Minor"


def test_remixer_is_extracted():
  track = dict(TRACK)
  track["remixers"] = [{"name": "Tom Santa"}]
  assert {c.field: c.value for c in parse_track(track)}["remixer"] == "Tom Santa"


def test_empty_track():
  assert parse_track({}) == []


def test_missing_fields_are_omitted_not_blanked():
  fields = {c.field for c in parse_track({"name": "X"})}
  assert fields == {"title"}


# --- token lifecycle -------------------------------------------------------


def test_token_is_refreshed_before_it_expires():
  """Tokens live 600s and runs last hours, so refresh is proactive."""
  nearly_expired = Token("abc", time.time() + REFRESH_MARGIN_S - 1)
  assert not nearly_expired.is_fresh
  healthy = Token("abc", time.time() + 600)
  assert healthy.is_fresh


def test_empty_token_is_never_fresh():
  assert not Token("", time.time() + 9999).is_fresh


def test_missing_credentials_fail_loudly(tmp_path):
  from music import db

  conn = db.connect(tmp_path / "b.db")
  db.migrate(conn)
  with pytest.raises(BeatportCredentialsError, match="partner portal"):
    Beatport(conn, "", "")
