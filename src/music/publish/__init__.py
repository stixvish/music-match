"""Publish: transcode, tag, name and file a finished track (SPEC.md §14)."""

import sqlite3
from pathlib import Path

from music.publish import naming, tag, transcode

__all__ = ["layout_path", "publish_track", "naming", "tag", "transcode"]

# six families (SPEC.md §7). unknown genres land in `other`.
FAMILIES = ("electronic", "hip-hop", "pop", "r&b-soul", "world", "other")


def layout_path(library: Path, family: str, artist: str, title: str) -> Path:
  """Build the output path.

  `library/<genre-family>/<artist>/<Artist> - <Title>.aiff`

  Args:
    library: Library root.
    family: Genre family; anything unrecognised becomes `other`.
    artist: Artist name.
    title: Track title.

  Returns:
    Full destination path.
  """
  safe_family = family if family in FAMILIES else "other"
  return (
    library
    / safe_family
    / naming.safe_component(artist)
    / naming.filename(artist, title)
  )


def resolved_tags(conn: sqlite3.Connection, track_id: int) -> tag.Tags:
  """Assemble the tag set for a track from its arbitration output.

  Args:
    conn: Open connection.
    track_id: Track to read.

  Returns:
    Tags containing every resolved field that maps to a frame.
  """
  tags = tag.Tags()
  rows = conn.execute(
    "SELECT field, value FROM resolved_field WHERE track_id = ?", (track_id,)
  ).fetchall()
  for row in rows:
    if row["field"] in tag.FRAME_MAP and row["value"]:
      tags[row["field"]] = row["value"]
  return tags


def publish_track(
  conn: sqlite3.Connection, track_id: int, library: Path, staging: Path
) -> Path:
  """Transcode, tag and file one track.

  Args:
    conn: Open connection.
    track_id: Track to publish.
    library: Library root.
    staging: Working directory for intermediates.

  Returns:
    The published path.

  Raises:
    RuntimeError: If the track or its source file is missing.
  """
  row = conn.execute(
    "SELECT t.id, t.genre_family, s.staging_path"
    " FROM track t JOIN source_file s ON s.id = t.source_file_id"
    " WHERE t.id = ?",
    (track_id,),
  ).fetchone()
  if row is None:
    raise RuntimeError(f"no track {track_id}")

  source = Path(row["staging_path"])
  if not source.exists():
    raise RuntimeError(f"source file missing: {source}")

  tags = resolved_tags(conn, track_id)
  artist = tags.values.get("artist", "unknown artist")
  title = tags.values.get("title", source.stem)

  dest = layout_path(library, row["genre_family"] or "other", artist, title)
  # collisions are flagged rather than silently overwritten (SPEC.md §14).
  if dest.exists():
    dest = dest.with_name(f"{dest.stem} (2){dest.suffix}")

  work = staging / f"{track_id}.aiff"
  transcode.to_aiff(source, work)

  art = transcode.extract_artwork(source, staging / f"{track_id}.jpg")
  if art:
    tags.artwork = art.read_bytes()
  # canonical form goes in the tag too, not just the filename (SPEC.md §14).
  if "title" in tags:
    tags["title"] = naming.canonical_title(tags["title"])
  tag.write(work, tags)

  dest.parent.mkdir(parents=True, exist_ok=True)
  work.replace(dest)
  conn.execute(
    "UPDATE track SET published_path=?, status='published',"
    " updated_at=datetime('now') WHERE id=?",
    (str(dest), track_id),
  )
  return dest
