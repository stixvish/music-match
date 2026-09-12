"""Turn resolved fields into a tag set (SPEC.md §10, §14).

The join between arbitration and the file: `resolved_field` rows in, an ID3v2.4
`Tags` object out. Canonical naming is applied here, not earlier, so the
database keeps the source's literal value and the file carries house style.
"""

import sqlite3
from dataclasses import dataclass

from music.publish import naming, tag

# resolved-field names that are not ID3 frames. They are kept in the database
# for provenance and re-tagging but never written to the file.
NON_TAG_FIELDS = frozenset(
  {
    "artwork_url",
    "catalog_number",
    "classifier_genre",
    "featured_artists",
    "genre_style",
    "mb_recording_id",
    "acoustid",
  }
)


# Every field the review ui offers, grouped for scanning and ordered for
# reading. Derived from `tag.FRAME_MAP` rather than hand-listed there and here:
# the ui was missing `grouping` and `original_artist` because the two drifted,
# and a field you cannot see is a field you cannot fill in.
FIELD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
  ("track", ("title", "artist", "mix_name", "remixer", "original_artist")),
  (
    "release",
    (
      "album",
      "album_artist",
      "track_number",
      "disc_number",
      "year",
      "release_date",
      "label",
      "isrc",
    ),
  ),
  ("classification", ("genre", "grouping", "key", "bpm")),
  ("credits", ("composer", "lyricist")),
  ("notes", ("comment",)),
  ("artwork", ("artwork_url",)),
)

# Writable but not in `tag.FRAME_MAP`, because `build` handles it specially.
# Deriving the ui's list from FRAME_MAP alone therefore missed it, and Rekordbox
# and Serato both display it (SPEC.md §10).
EXTRA_WRITABLE: tuple[str, ...] = ("comment",)

EDITABLE_ORDER: tuple[str, ...] = tuple(
  field for _group, names in FIELD_GROUPS for field in names
)


@dataclass(frozen=True)
class Built:
  """A tag set plus what was left out and why."""

  tags: tag.Tags
  skipped: tuple[str, ...] = ()


def build(resolved: dict[str, str]) -> Built:
  """Build a tag set from resolved fields.

  Canonical naming (SPEC.md §14) is applied to the title here so the database
  keeps whatever the winning source actually said, while the file gets house
  style. The two must not be conflated — one is evidence, one is presentation.

  Args:
    resolved: Field name to winning value.

  Returns:
    The tags, and any fields that had no frame to go in.
  """
  tags = tag.Tags()
  skipped: list[str] = []

  for field, value in sorted(resolved.items()):
    if not value:
      continue
    if field == "comment":
      tags.comment = value
      continue
    if field in NON_TAG_FIELDS:
      skipped.append(field)
      continue
    if field not in tag.FRAME_MAP:
      skipped.append(field)
      continue
    tags[field] = value

  if "title" in tags:
    tags["title"] = naming.canonical_title(tags["title"])
  return Built(tags=tags, skipped=tuple(skipped))


def load_resolved(conn: sqlite3.Connection, track_id: int) -> dict[str, str]:
  """Read a track's resolved fields.

  Args:
    conn: Open connection.
    track_id: Track to read.

  Returns:
    Field name to value.
  """
  return {
    row["field"]: row["value"]
    for row in conn.execute(
      "SELECT field, value FROM resolved_field WHERE track_id = ?", (track_id,)
    )
    if row["value"]
  }


def build_for(conn: sqlite3.Connection, track_id: int) -> Built:
  """Build a track's tag set straight from the database.

  Args:
    conn: Open connection.
    track_id: Track to build for.

  Returns:
    The tags and any skipped fields.
  """
  return build(load_resolved(conn, track_id))
