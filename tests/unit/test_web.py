"""Tests for the web layer's logic.

The pages themselves are Vue components rendered by NiceGUI; what is
worth testing here is everything underneath them — what goes in the
queue, what a reviewer is shown as changing, and what an edited form
means. The UI deliberately holds no decisions of its own, so testing this
layer covers the behaviour.
"""

import json
import pathlib

import pytest

from music_match.db import queries
from music_match.tagging import tags as tag_io
from music_match.tagging.fields import TrackTags
from music_match.web import app as web_app
from music_match.web import state as web_state


@pytest.fixture(name="settings")
def fixture_settings(tmp_path: pathlib.Path) -> web_state.Settings:
  """Builds settings pointing at a temporary database.

  Args:
    tmp_path: pytest's per-test temporary directory.

  Returns:
    The settings.
  """
  return web_state.Settings(db=tmp_path / "state.db",
                            sources=tmp_path / "sources.toml",
                            art_store=tmp_path / "art")


def seed(settings: web_state.Settings,
         path: pathlib.Path,
         status: str,
         proposed: dict[str, object] | None = None,
         confidence: float = 0.6) -> int:
  """Adds a track in a given state.

  Args:
    settings: Where to write.
    path: The track's file.
    status: The match status to record.
    proposed: The proposed tags, if any.
    confidence: The match confidence.

  Returns:
    The track's row id.
  """
  conn = settings.open_db()
  try:
    track_id = queries.upsert_track(conn, path=path, source_name="yt-dlp")
    if proposed is not None:
      queries.record_match(conn,
                           track_id=track_id,
                           source="spotify",
                           confidence=confidence,
                           status=status,
                           tags_json=json.dumps(proposed),
                           art_url=None)
    else:
      queries.set_match_status(conn, track_id, status)
    conn.commit()
    return track_id
  finally:
    conn.close()


def test_the_queue_holds_the_three_states_needing_a_decision(
    settings: web_state.Settings, tmp_path: pathlib.Path) -> None:
  """Uncertain, unmatched and quarantined all need a human."""
  seed(settings, tmp_path / "a.m4a", "review", {"title": "A"})
  seed(settings, tmp_path / "b.m4a", "no_match", {"title": "B"}, 0.1)
  seed(settings, tmp_path / "c.m4a", "quarantined")
  statuses = {item.status for item in web_state.load_queue(settings)}
  assert statuses == {"review", "no_match", "quarantined"}


def test_a_matched_track_is_not_in_the_queue(settings: web_state.Settings,
                                             tmp_path: pathlib.Path) -> None:
  """A confident match needs nothing from anyone."""
  seed(settings, tmp_path / "a.m4a", "matched", {"title": "A"}, 0.95)
  assert not web_state.load_queue(settings)


def test_the_least_confident_comes_first(settings: web_state.Settings,
                                         tmp_path: pathlib.Path) -> None:
  """The ones most likely to be wrong are the ones worth seeing."""
  seed(settings, tmp_path / "high.m4a", "review", {"title": "H"}, 0.8)
  seed(settings, tmp_path / "low.m4a", "review", {"title": "L"}, 0.5)
  assert web_state.load_queue(settings)[0].name() == "low.m4a"


def test_counts_include_zeros(settings: web_state.Settings,
                              tmp_path: pathlib.Path) -> None:
  """A stable set of badges beats ones that appear and vanish."""
  seed(settings, tmp_path / "a.m4a", "review", {"title": "A"})
  assert web_state.counts(settings) == {
      "review": 1,
      "no_match": 0,
      "quarantined": 0
  }


def test_differences_show_only_what_would_change(
    settings: web_state.Settings, m4a_file: pathlib.Path) -> None:
  """A reviewer needs what is at stake, not every field."""
  tag_io.write_tags(m4a_file, TrackTags(title="Old", artist="Same"))
  seed(settings, m4a_file, "review", {"title": "New", "artist": "Same"})
  item = web_state.load_queue(settings)[0]
  assert item.differences() == [("title", "Old", "New")]


def test_current_values_come_from_the_file_when_unsnapshotted(
    settings: web_state.Settings, m4a_file: pathlib.Path) -> None:
  """Only `reindex` writes the snapshot, so a scanned library has none.

  Showing an empty "current" column beside a full "proposed" one would
  tell a reviewer the file is untagged when it is not, and turn zero
  real changes into four phantom ones.
  """
  tag_io.write_tags(m4a_file, TrackTags(title="Hello", artist="Adele"))
  seed(settings, m4a_file, "review", {"title": "Hello", "artist": "Adele"})
  item = web_state.load_queue(settings)[0]
  assert item.current.title == "Hello"
  assert not item.differences()


def test_a_missing_file_does_not_break_the_queue(
    settings: web_state.Settings, tmp_path: pathlib.Path) -> None:
  """A file deleted since the scan must not take the page down."""
  seed(settings, tmp_path / "gone.m4a", "review", {"title": "A"})
  item = web_state.load_queue(settings)[0]
  assert item.current.is_empty()


def test_accepting_writes_the_tags(settings: web_state.Settings,
                                   m4a_file: pathlib.Path) -> None:
  """Accepting in the browser writes exactly as the CLI does."""
  tag_io.write_tags(m4a_file, TrackTags(title="Old"))
  track_id = seed(settings, m4a_file, "review", {"title": "New"})
  web_state.accept(settings, track_id, TrackTags(title="New"), None)
  assert tag_io.read_tags(m4a_file).title == "New"


def test_accepting_records_history(settings: web_state.Settings,
                                   m4a_file: pathlib.Path) -> None:
  """A web edit must be as undoable as a command-line one."""
  tag_io.write_tags(m4a_file, TrackTags(title="Old"))
  track_id = seed(settings, m4a_file, "review", {"title": "New"})
  web_state.accept(settings, track_id, TrackTags(title="New"), None)
  conn = settings.open_db()
  try:
    history = queries.history_for_track(conn, track_id)
  finally:
    conn.close()
  assert [(row["field"], row["old_value"], row["new_value"]) for row in history
         ] == [("title", "Old", "New")]


def test_accepting_takes_the_track_out_of_the_queue(
    settings: web_state.Settings, m4a_file: pathlib.Path) -> None:
  """Otherwise it comes straight back on the next refresh."""
  track_id = seed(settings, m4a_file, "review", {"title": "New"})
  web_state.accept(settings, track_id, TrackTags(title="New"), None)
  assert not web_state.load_queue(settings)


def test_accepting_a_vanished_track_is_reported(
    settings: web_state.Settings) -> None:
  """A stale browser tab must not raise."""
  message = web_state.accept(settings, 999, TrackTags(title="X"), None)
  assert "no longer" in message


def test_rejecting_marks_it_wont_match(settings: web_state.Settings,
                                       tmp_path: pathlib.Path) -> None:
  """A self-made edit should stop reappearing forever."""
  track_id = seed(settings, tmp_path / "a.m4a", "review", {"title": "A"})
  web_state.reject(settings, track_id, "self-made")
  assert not web_state.load_queue(settings)


def test_releasing_returns_a_quarantined_track(settings: web_state.Settings,
                                               tmp_path: pathlib.Path) -> None:
  """Confirming the audio is fine puts it back in the pipeline."""
  track_id = seed(settings, tmp_path / "a.m4a", "quarantined")
  web_state.release(settings, track_id)
  conn = settings.open_db()
  try:
    row = queries.track_by_id(conn, track_id)
  finally:
    conn.close()
  assert row is not None
  assert row["match_status"] == "pending"


def test_corrupt_stored_tags_do_not_break_the_page(
    settings: web_state.Settings, tmp_path: pathlib.Path) -> None:
  """A malformed JSON column renders as empty rather than raising."""
  track_id = seed(settings, tmp_path / "a.m4a", "review", {"title": "A"})
  conn = settings.open_db()
  try:
    conn.execute("UPDATE tracks SET matched_tags_json = ? WHERE id = ?",
                 ("{not json", track_id))
    conn.commit()
  finally:
    conn.close()
  assert web_state.load_queue(settings)[0].proposed.is_empty()


# --------------------------------------------------------- form handling


class FakeInput:
  """Stands in for a NiceGUI input element."""

  def __init__(self, value: str) -> None:
    """Records the value the form holds.

    Args:
      value: What the field contains.
    """
    self.value = value


def test_form_values_become_tags() -> None:
  """What a reviewer typed is what gets written."""
  tags = web_app.tags_from_inputs({
      "title": FakeInput("Strobe"),
      "artist": FakeInput("deadmau5"),
  })
  assert tags.title == "Strobe"
  assert tags.artist == "deadmau5"


def test_a_blank_field_means_no_opinion() -> None:
  """Clearing a field leaves the file's value alone.

  That is what an unset field means everywhere else in this tool, and the
  form should not be the one place it means "erase this".
  """
  tags = web_app.tags_from_inputs({
      "title": FakeInput("Strobe"),
      "album": FakeInput("   "),
  })
  assert tags.album is None


def test_numeric_fields_are_parsed() -> None:
  """A year typed into a text box has to reach the file as a number."""
  assert web_app.tags_from_inputs({"year": FakeInput("2009")}).year == 2009


def test_a_nonsense_number_is_dropped_not_written() -> None:
  """Writing "twenty-oh-nine" into a year field helps nobody."""
  assert web_app.tags_from_inputs({"year": FakeInput("twenty")}).year is None


def test_the_internal_id_is_not_editable() -> None:
  """Bookkeeping is not a person's business to change."""
  assert "source_video_id" not in web_app.EDITABLE_FIELDS


def test_every_status_in_the_queue_is_explained() -> None:
  """The three states need different answers, so they need labels."""
  assert set(web_app.STATUS_HELP) == {"review", "no_match", "quarantined"}
