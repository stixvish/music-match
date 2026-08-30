"""Writing matched metadata to files, and undoing it.

The ordering rule this module exists to enforce: **history is recorded
before the file is touched.** If a run dies between the two, the worst
case is a history row for a change that did not happen — visible, and
harmless to replay. The other order loses the previous value forever,
which is exactly what undo depends on.

Album art travels through the same batch as the text fields, so a single
undo restores both together rather than leaving a file with last week's
title and this week's cover.
"""

import dataclasses
import pathlib
import sqlite3
import uuid
from typing import Any, Mapping

from music_match.db import queries
from music_match.tagging import art as art_lib
from music_match.tagging import tags as tag_io
from music_match.tagging.fields import NUMERIC_FIELDS
from music_match.tagging.fields import TrackTags

# The pseudo-field album art is recorded under in `tag_history`. Its
# stored values are content hashes, never image bytes.
ART_FIELD = "album_art"


@dataclasses.dataclass(frozen=True)
class WriteResult:
  """What one write did, or would have done.

  Attributes:
    batch: The identifier shared by this write's history rows.
    changes: Field name to (old value, new value), including `album_art`
      whose values are content hashes.
    wrote: Whether the file was actually modified.
  """
  batch: str
  changes: Mapping[str, tuple[Any, Any]]
  wrote: bool

  def is_noop(self) -> bool:
    """Returns whether nothing needed changing."""
    return not self.changes


def new_batch() -> str:
  """Returns a fresh identifier for one write."""
  return uuid.uuid4().hex


def apply_tags(connection: sqlite3.Connection,
               *,
               track_id: int,
               path: pathlib.Path,
               tags: TrackTags,
               art_hash: str | None = None,
               store: art_lib.ArtStore | None = None,
               dry_run: bool = False) -> WriteResult:
  """Writes matched tags to a file, recording the previous values first.

  Args:
    connection: An open database connection.
    track_id: The track's row id.
    path: The audio file to write.
    tags: The tags to apply. Unset fields are left alone.
    art_hash: Content hash of the cover to embed, if any.
    store: Where stored art lives. Required when `art_hash` is given.
    dry_run: Compute the changes without writing anything, to the file or
      to the database.

  Returns:
    What changed, or would have.

  Raises:
    TagError: If the file cannot be read or written.
    ArtError: If the cover cannot be read from the store.
  """
  changes: dict[str,
                tuple[Any,
                      Any]] = dict(tag_io.write_tags(path, tags, dry_run=True))

  image: bytes | None = None
  current_art_hash: str | None = None
  if art_hash is not None:
    if store is None:
      raise art_lib.ArtError("an art store is required to embed cover art")
    image = store.get(art_hash)
    current = tag_io.read_art(path)
    if current:
      # The cover about to be replaced goes into the store *first*.
      # Recording only its hash would leave undo holding an address with
      # nothing behind it — the one thing the content-addressed store
      # exists to prevent.
      current_art_hash = store.put(current)
    if current_art_hash != art_hash:
      changes[ART_FIELD] = (current_art_hash, art_hash)

  batch = new_batch()
  if dry_run or not changes:
    return WriteResult(batch=batch, changes=changes, wrote=False)

  # Recorded before the write, deliberately. See the module docstring.
  queries.log_changes(connection,
                      track_id=track_id,
                      batch=batch,
                      changes=changes)
  connection.commit()

  tag_io.write_tags(path, tags)
  if ART_FIELD in changes and image is not None:
    tag_io.write_art(path, image)
  return WriteResult(batch=batch, changes=changes, wrote=True)


def revert(connection: sqlite3.Connection,
           *,
           track_id: int,
           path: pathlib.Path,
           batch: str,
           store: art_lib.ArtStore | None = None,
           dry_run: bool = False) -> WriteResult:
  """Restores a file to the state it held before a given write.

  Args:
    connection: An open database connection.
    track_id: The track's row id.
    path: The audio file to restore.
    batch: The write to roll back to, inclusive. Everything from that
      point on is undone.
    store: Where stored art lives, needed if art is being restored.
    dry_run: Report what would be restored without writing.

  Returns:
    What was restored, or would be. The undo is itself recorded as a new
    batch, so it can be undone in turn.

  Raises:
    TagError: If the file cannot be read or written.
    ArtError: If a stored cover is needed and missing.
  """
  restore = queries.values_to_restore(connection, track_id, batch)
  if not restore:
    return WriteResult(batch=batch, changes={}, wrote=False)

  art_target = restore.pop(ART_FIELD, "unchanged")
  current = tag_io.read_tags(path)
  existing = current.as_dict(include_empty=True)

  changes: dict[str, tuple[Any, Any]] = {}
  restored_fields: dict[str, Any] = {}
  for field, raw in restore.items():
    # History stores everything as text; numerics have to come back as
    # integers or they compare unequal to what is on the file and a
    # redundant change gets recorded.
    value = _coerce(field, raw) if raw is not None else None
    if existing.get(field) == value:
      continue
    changes[field] = (existing.get(field), value)
    restored_fields[field] = value

  image: bytes | None = None
  if art_target != "unchanged":
    embedded = tag_io.read_art(path)
    # Preserve the cover being replaced by the undo, so the undo can
    # itself be undone.
    current_hash = store.put(embedded) if embedded and store else (
        art_lib.digest(embedded) if embedded else None)
    if current_hash != art_target:
      changes[ART_FIELD] = (current_hash, art_target)
      if art_target is not None:
        if store is None:
          raise art_lib.ArtError(
              "an art store is required to restore cover art")
        image = store.get(str(art_target))

  undo_batch = new_batch()
  if dry_run or not changes:
    return WriteResult(batch=undo_batch, changes=changes, wrote=False)

  queries.log_changes(connection,
                      track_id=track_id,
                      batch=undo_batch,
                      changes=changes)
  connection.commit()

  _write_restored(path, restored_fields)
  if ART_FIELD in changes:
    tag_io.write_art(path, image)
  return WriteResult(batch=undo_batch, changes=changes, wrote=True)


def _write_restored(path: pathlib.Path, values: Mapping[str, Any]) -> None:
  """Writes restored values back onto a file.

  A field being restored to None means it was unset before the change
  being undone. `write_tags` treats None as "no opinion" rather than
  "clear", so those are written as empty strings instead — the closest a
  tag container gets to absent.

  Args:
    path: The audio file.
    values: Field name to the value to restore.
  """
  if not values:
    return
  written = {
      field: ("" if value is None else value) for field, value in values.items()
  }
  tag_io.write_tags(path, TrackTags.from_mapping(written))


def _coerce(field: str, value: Any) -> Any:
  """Converts a stored history value back to the field's own type.

  History stores everything as text; the numeric fields need to come back
  as integers or they will not compare equal to what is on the file.

  Args:
    field: The field name.
    value: Its stored text value.

  Returns:
    The value, as an int for numeric fields where that parses.
  """
  if field not in NUMERIC_FIELDS:
    return value
  try:
    return int(str(value))
  except ValueError:
    return value
