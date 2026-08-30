"""Tests for writing matched tags, and undoing the write.

The invariant these exist to protect: history is recorded *before* the
file is touched, and every value it records can actually be restored.
"""

import io
import pathlib
import sqlite3

import pytest
from PIL import Image

from music_match.db import connection
from music_match.db import queries
from music_match.tagging import apply as apply_lib
from music_match.tagging import art as art_lib
from music_match.tagging import tags as tag_io
from music_match.tagging.fields import TrackTags


def cover(colour: tuple[int, int, int] = (10, 120, 200)) -> bytes:
  """Builds a normalised cover image.

  Args:
    colour: Fill colour, so tests can make distinguishable covers.

  Returns:
    The normalised JPEG bytes.
  """
  buffer = io.BytesIO()
  Image.new("RGB", (300, 300), colour).save(buffer, "PNG")
  return art_lib.normalise(buffer.getvalue())


@pytest.fixture(name="workspace")
def fixture_workspace(
    tmp_path: pathlib.Path, m4a_file: pathlib.Path
) -> tuple[sqlite3.Connection, pathlib.Path, int, art_lib.ArtStore]:
  """Sets up a database, an indexed file and an art store.

  Args:
    tmp_path: pytest's per-test temporary directory.
    m4a_file: A tiny audio file.

  Returns:
    The connection, the file, its track id, and the art store.
  """
  tag_io.write_tags(
      m4a_file, TrackTags(title="Old Title", artist="Old Artist", year=1999))
  conn = connection.connect(tmp_path / "state.db")
  connection.initialize(conn)
  track_id = queries.upsert_track(conn, path=m4a_file, source_name="yt-dlp")
  conn.commit()
  return (conn, m4a_file, track_id, art_lib.ArtStore(tmp_path / "art"))


def test_dry_run_touches_nothing(workspace: tuple) -> None:
  """A dry run reports the diff and leaves both file and history alone."""
  conn, path, track_id, _ = workspace
  before = path.read_bytes()
  result = apply_lib.apply_tags(conn,
                                track_id=track_id,
                                path=path,
                                tags=TrackTags(title="New Title"),
                                dry_run=True)
  assert result.changes == {"title": ("Old Title", "New Title")}
  assert not result.wrote
  assert path.read_bytes() == before
  assert not queries.history_for_track(conn, track_id)


def test_history_is_recorded(workspace: tuple) -> None:
  """The previous value is what undo depends on, so it must be stored."""
  conn, path, track_id, _ = workspace
  apply_lib.apply_tags(conn,
                       track_id=track_id,
                       path=path,
                       tags=TrackTags(title="New Title"))
  rows = queries.history_for_track(conn, track_id)
  assert [(r["field"], r["old_value"], r["new_value"]) for r in rows
         ] == [("title", "Old Title", "New Title")]


def test_unset_fields_are_left_alone(workspace: tuple) -> None:
  """Writing a title must not wipe an artist the match said nothing about."""
  conn, path, track_id, _ = workspace
  apply_lib.apply_tags(conn,
                       track_id=track_id,
                       path=path,
                       tags=TrackTags(title="New Title"))
  assert tag_io.read_tags(path).artist == "Old Artist"


def test_a_repeat_write_changes_nothing(workspace: tuple) -> None:
  """Applying the same tags twice records no second history entry."""
  conn, path, track_id, _ = workspace
  tags = TrackTags(title="New Title")
  apply_lib.apply_tags(conn, track_id=track_id, path=path, tags=tags)
  second = apply_lib.apply_tags(conn, track_id=track_id, path=path, tags=tags)
  assert second.is_noop()
  assert len(queries.history_for_track(conn, track_id)) == 1


def test_art_is_embedded_and_recorded_as_a_hash(workspace: tuple) -> None:
  """History holds the content address, never the image bytes."""
  conn, path, track_id, store = workspace
  art_hash = store.put(cover())
  apply_lib.apply_tags(conn,
                       track_id=track_id,
                       path=path,
                       tags=TrackTags(),
                       art_hash=art_hash,
                       store=store)
  assert art_lib.digest(tag_io.read_art(path)) == art_hash
  rows = queries.history_for_track(conn, track_id)
  assert rows[0]["field"] == apply_lib.ART_FIELD
  assert rows[0]["new_value"] == art_hash


def test_the_replaced_cover_is_kept(workspace: tuple) -> None:
  """Recording a hash with nothing behind it would make undo impossible.

  This is the whole point of the content-addressed store: the cover about
  to be overwritten has to go into it *before* the write.
  """
  conn, path, track_id, store = workspace
  original = cover((255, 0, 0))
  tag_io.write_art(path, original)
  replacement = store.put(cover((0, 0, 255)))

  apply_lib.apply_tags(conn,
                       track_id=track_id,
                       path=path,
                       tags=TrackTags(),
                       art_hash=replacement,
                       store=store)
  assert store.has(art_lib.digest(original))
  assert store.get(art_lib.digest(original)) == original


def test_undo_restores_text_and_art_together(workspace: tuple) -> None:
  """One undo puts everything back, not the text at one point and art
  at another."""
  conn, path, track_id, store = workspace
  original = cover((255, 0, 0))
  tag_io.write_art(path, original)
  replacement = store.put(cover((0, 0, 255)))

  result = apply_lib.apply_tags(conn,
                                track_id=track_id,
                                path=path,
                                tags=TrackTags(title="New Title"),
                                art_hash=replacement,
                                store=store)
  apply_lib.revert(conn,
                   track_id=track_id,
                   path=path,
                   batch=result.batch,
                   store=store)
  assert tag_io.read_tags(path).title == "Old Title"
  assert tag_io.read_art(path) == original


def test_undo_goes_back_past_several_writes(workspace: tuple) -> None:
  """Undoing three edits returns the value from before the first.

  Taking the most recent `old_value` would only step back one edit.
  """
  conn, path, track_id, _ = workspace
  first = apply_lib.apply_tags(conn,
                               track_id=track_id,
                               path=path,
                               tags=TrackTags(title="B"))
  apply_lib.apply_tags(conn,
                       track_id=track_id,
                       path=path,
                       tags=TrackTags(title="C"))
  apply_lib.apply_tags(conn,
                       track_id=track_id,
                       path=path,
                       tags=TrackTags(title="D"))
  apply_lib.revert(conn, track_id=track_id, path=path, batch=first.batch)
  assert tag_io.read_tags(path).title == "Old Title"


def test_undo_restores_numeric_fields_as_numbers(workspace: tuple) -> None:
  """History stores text; a year has to come back as an integer."""
  conn, path, track_id, _ = workspace
  result = apply_lib.apply_tags(conn,
                                track_id=track_id,
                                path=path,
                                tags=TrackTags(year=2009))
  apply_lib.revert(conn, track_id=track_id, path=path, batch=result.batch)
  assert tag_io.read_tags(path).year == 1999


def test_an_undo_is_itself_undoable(workspace: tuple) -> None:
  """The revert is recorded as its own batch."""
  conn, path, track_id, _ = workspace
  applied = apply_lib.apply_tags(conn,
                                 track_id=track_id,
                                 path=path,
                                 tags=TrackTags(title="New Title"))
  undone = apply_lib.revert(conn,
                            track_id=track_id,
                            path=path,
                            batch=applied.batch)
  batches = {batch for batch, _, _ in queries.batches_for_track(conn, track_id)}
  assert {applied.batch, undone.batch} <= batches


def test_undo_dry_run_touches_nothing(workspace: tuple) -> None:
  """A dry-run undo reports the restore without performing it."""
  conn, path, track_id, _ = workspace
  applied = apply_lib.apply_tags(conn,
                                 track_id=track_id,
                                 path=path,
                                 tags=TrackTags(title="New Title"))
  result = apply_lib.revert(conn,
                            track_id=track_id,
                            path=path,
                            batch=applied.batch,
                            dry_run=True)
  assert result.changes
  assert not result.wrote
  assert tag_io.read_tags(path).title == "New Title"


def test_undo_with_no_history_is_harmless(workspace: tuple) -> None:
  """Reverting a batch that changed nothing is a no-op, not an error."""
  conn, path, track_id, _ = workspace
  result = apply_lib.revert(conn,
                            track_id=track_id,
                            path=path,
                            batch="never-existed")
  assert result.is_noop()


def test_embedding_art_needs_a_store(workspace: tuple) -> None:
  """A hash without a store to resolve it is a caller error."""
  conn, path, track_id, _ = workspace
  with pytest.raises(art_lib.ArtError, match="art store is required"):
    apply_lib.apply_tags(conn,
                         track_id=track_id,
                         path=path,
                         tags=TrackTags(),
                         art_hash="0" * 64,
                         store=None)
