"""Publish: transcode, tag, name and file a finished track (SPEC.md §14)."""

import logging
import sqlite3
from pathlib import Path

from music.publish import fields, naming, tag, transcode

log = logging.getLogger(__name__)

__all__ = [
  "fetch_artwork",
  "fields",
  "layout_path",
  "naming",
  "publish_track",
  "tag",
  "transcode",
]

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


def fetch_artwork(url: str, timeout: int = 20) -> bytes | None:
  """Download cover art.

  iTunes serves the highest-quality artwork of the free sources (§7), and its
  URLs are rewritten to full size before they reach here.

  Args:
    url: Image URL.
    timeout: Seconds to wait.

  Returns:
    Image bytes, or None if the fetch fails. Artwork is never worth failing a
    publish over.
  """
  import urllib.request

  try:
    request = urllib.request.Request(url, headers={"User-Agent": "music-match/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
      return bytes(response.read())
  except Exception as exc:  # noqa: BLE001 - cosmetic, never fatal
    log.debug("artwork fetch failed for %s: %s", url[:60], exc)
    return None


def resolved_tags(conn: sqlite3.Connection, track_id: int) -> tag.Tags:
  """Assemble the tag set for a track from its arbitration output.

  Args:
    conn: Open connection.
    track_id: Track to read.

  Returns:
    Tags containing every resolved field that maps to a frame.
  """
  return fields.build_for(conn, track_id).tags


def retag(conn: sqlite3.Connection, track_id: int) -> Path | None:
  """Rewrite an already-published file's tags from the database.

  This is the payoff for treating the database as the source of truth: a better
  resolver re-tags the library without re-downloading or re-transcoding
  anything. Manual edits survive because arbitration never overwrote them
  (SPEC.md §15).

  The file is also renamed if canonical naming now produces a different name.

  Args:
    conn: Open connection.
    track_id: Track to re-tag.

  Returns:
    The file's path, or None if the track has not been published.
  """
  row = conn.execute(
    "SELECT published_path, genre_family FROM track WHERE id = ?", (track_id,)
  ).fetchone()
  if row is None or not row["published_path"]:
    return None
  path = Path(row["published_path"])
  if not path.exists():
    log.warning("published file missing, cannot retag: %s", path)
    return None

  built = fields.build_for(conn, track_id)
  # keep whatever artwork the file already has rather than re-fetching it
  existing = tag.read(path)
  if existing.artwork and not built.tags.artwork:
    built.tags.artwork = existing.artwork
  tag.write(path, built.tags)

  renamed = _rename_if_needed(conn, track_id, path, row["genre_family"], built.tags)
  return renamed or path


def _rename_if_needed(
  conn: sqlite3.Connection,
  track_id: int,
  path: Path,
  family: str | None,
  tags: tag.Tags,
) -> Path | None:
  """Move a published file if canonical naming now yields a different path."""
  artist = tags.values.get("artist")
  title = tags.values.get("title")
  if not artist or not title:
    return None
  target = layout_path(path.parents[2], family or "other", artist, title)
  if target == path or target.exists():
    return None
  target.parent.mkdir(parents=True, exist_ok=True)
  path.replace(target)
  conn.execute(
    "UPDATE track SET published_path = ?, updated_at = datetime('now') WHERE id = ?",
    (str(target), track_id),
  )
  return target


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

  resolved = fields.load_resolved(conn, track_id)
  art_url = resolved.get("artwork_url", "")
  if art_url:
    tags.artwork = fetch_artwork(art_url)
  if not tags.artwork:
    # fall back to whatever the downloaded file already carried
    art = transcode.extract_artwork(source, staging / f"{track_id}.jpg")
    if art:
      tags.artwork = art.read_bytes()
  tag.write(work, tags)

  dest.parent.mkdir(parents=True, exist_ok=True)
  work.replace(dest)
  conn.execute(
    "UPDATE track SET published_path=?, status='published',"
    " updated_at=datetime('now') WHERE id=?",
    (str(dest), track_id),
  )
  return dest
