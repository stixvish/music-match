"""What the web pages need to reach the rest of the tool.

The UI is deliberately thin: it renders, and it calls the same functions
the CLI calls. Nothing here decides what a good match is or how a tag is
written — that lives in `matching` and `tagging`, and the web layer would
drift from the CLI the moment it grew its own copy.
"""

import dataclasses
import json
import pathlib
import sqlite3
from typing import Any

from music_match.config import loader
from music_match.db import connection
from music_match.db import queries
from music_match.tagging import apply as apply_lib
from music_match.tagging import art as art_lib
from music_match.tagging import tags as tag_io
from music_match.tagging.fields import TrackTags


@dataclasses.dataclass(frozen=True)
class Settings:
  """Where the running app reads its state from.

  Attributes:
    db: The SQLite database.
    sources: The sources.toml path.
    art_store: Where cover art is kept.
  """
  db: pathlib.Path = connection.DEFAULT_DB_FILE
  sources: pathlib.Path = loader.DEFAULT_SOURCES_FILE
  art_store: pathlib.Path = art_lib.DEFAULT_STORE_DIR

  def open_db(self) -> sqlite3.Connection:
    """Opens an initialized connection to the configured database.

    Returns:
      The connection. The caller closes it.
    """
    conn = connection.connect(self.db)
    connection.initialize(conn)
    return conn


@dataclasses.dataclass(frozen=True)
class ReviewItem:
  """One track awaiting a decision, flattened for display.

  Attributes:
    track_id: The track's row id.
    path: Where the file is.
    status: Why it is in the queue.
    confidence: The match confidence, if there was a match.
    source: Which source supplied most of the proposal.
    proposed: The proposed tags.
    current: The file's tags as last recorded.
    art_url: A cover image the match found, if any.
    genre: The locally detected genre, if any.
  """
  track_id: int
  path: pathlib.Path
  status: str
  confidence: float | None
  source: str | None
  proposed: TrackTags
  current: TrackTags
  art_url: str | None
  genre: str | None

  def name(self) -> str:
    """Returns the file name, which is how a person identifies a track."""
    return self.path.name

  def differences(self) -> list[tuple[str, str, str]]:
    """Returns the fields the proposal would change.

    Returns:
      (field, current value, proposed value) for each difference, so a
      reviewer sees what is actually at stake rather than every field.
    """
    current = self.current.as_dict(include_empty=True)
    rows = []
    for field, value in self.proposed.as_dict().items():
      before = current.get(field)
      if before != value:
        rows.append((field, "" if before is None else str(before), str(value)))
    return rows


def _tags_from_json(raw: Any) -> TrackTags:
  """Rebuilds tags from a stored JSON column.

  Args:
    raw: The column value, which may be None or malformed.

  Returns:
    The tags, empty if there was nothing usable to read.
  """
  if not raw:
    return TrackTags()
  try:
    values = json.loads(raw)
  except (TypeError, json.JSONDecodeError):
    return TrackTags()
  return TrackTags.from_mapping(values) if isinstance(values,
                                                      dict) else TrackTags()


def current_tags(row: sqlite3.Row, path: pathlib.Path) -> TrackTags:
  """Returns what a file's tags currently are.

  Prefers the stored snapshot, and falls back to reading the file. Only
  `reindex` writes the snapshot, so a library that was merely scanned has
  none — and showing a reviewer an empty "current" column next to a full
  "proposed" one would suggest the file is untagged when it is not.

  Args:
    row: The track's database row.
    path: Where the file is.

  Returns:
    The file's current tags, empty if it cannot be read.
  """
  keys = row.keys()
  stored = _tags_from_json(row["tags_json"] if "tags_json" in keys else None)
  if not stored.is_empty():
    return stored
  try:
    return tag_io.read_tags(path)
  except tag_io.TagError:
    return TrackTags()


def item_from_row(row: sqlite3.Row) -> ReviewItem:
  """Flattens a database row into something a page can render.

  Args:
    row: A row from the review queue.

  Returns:
    The review item.
  """
  path = pathlib.Path(row["path"])
  return ReviewItem(
      track_id=int(row["id"]),
      path=path,
      status=str(row["match_status"]),
      confidence=row["match_confidence"],
      source=row["matched_source"],
      proposed=_tags_from_json(row["matched_tags_json"]),
      current=current_tags(row, path),
      art_url=row["matched_art_url"],
      genre=row["detected_genre"],
  )


def load_queue(settings: Settings,
               statuses: tuple[str, ...] | None = None) -> list[ReviewItem]:
  """Loads the tracks waiting on a decision.

  Args:
    settings: Where to read from.
    statuses: Which statuses to include.

  Returns:
    The queue, least confident first.
  """
  conn = settings.open_db()
  try:
    return [item_from_row(row) for row in queries.review_queue(conn, statuses)]
  finally:
    conn.close()


def counts(settings: Settings) -> dict[str, int]:
  """Returns how many tracks are in each state needing a decision.

  Args:
    settings: Where to read from.

  Returns:
    Status to count.
  """
  conn = settings.open_db()
  try:
    return queries.review_counts(conn)
  finally:
    conn.close()


def accept(settings: Settings, track_id: int, tags: TrackTags,
           art_url: str | None) -> str:
  """Writes a reviewed proposal to the file.

  Goes through the same `apply` path the CLI uses, so the previous values
  are recorded in `tag_history` before the file is touched and `undo`
  works on a web edit exactly as it does on a command-line one.

  Args:
    settings: Where to read and write.
    track_id: The track's row id.
    tags: The tags to write, as edited by the reviewer.
    art_url: Cover art to fetch and embed, if any.

  Returns:
    A short message describing what happened.
  """
  conn = settings.open_db()
  try:
    row = queries.track_by_id(conn, track_id)
    if row is None:
      return "that track is no longer in the database"
    path = pathlib.Path(row["path"])
    store = art_lib.ArtStore(settings.art_store)
    art_hash = None
    if art_url:
      try:
        art_hash = store.store_url(art_url)
      except art_lib.ArtError as err:
        return f"tags not written: could not fetch cover art ({err})"
    result = apply_lib.apply_tags(conn,
                                  track_id=track_id,
                                  path=path,
                                  tags=tags,
                                  art_hash=art_hash,
                                  store=store)
    queries.record_match(conn,
                         track_id=track_id,
                         source=row["matched_source"],
                         confidence=row["match_confidence"] or 1.0,
                         status="matched",
                         tags_json=json.dumps(tags.as_dict()),
                         art_url=art_url)
    queries.record_snapshot(conn, track_id, json.dumps(tags.as_dict()))
    conn.commit()
    if result.is_noop():
      return "nothing to change; the file already matches"
    return f"wrote {len(result.changes)} field(s) to {path.name}"
  finally:
    conn.close()


def reject(settings: Settings, track_id: int, reason: str | None = None) -> str:
  """Marks a track as never going to match.

  Args:
    settings: Where to write.
    track_id: The track's row id.
    reason: Why, for the record.

  Returns:
    A short message describing what happened.
  """
  conn = settings.open_db()
  try:
    queries.mark_wont_match(conn, track_id, reason)
    conn.commit()
    return "marked as won't match; it will stop appearing here"
  finally:
    conn.close()


def release(settings: Settings, track_id: int) -> str:
  """Returns a quarantined track to the pipeline.

  Args:
    settings: Where to write.
    track_id: The track's row id.

  Returns:
    A short message describing what happened.
  """
  conn = settings.open_db()
  try:
    queries.set_match_status(conn, track_id, "pending")
    conn.commit()
    return "released; it will be matched on the next run"
  finally:
    conn.close()
