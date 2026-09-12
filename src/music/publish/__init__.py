"""Publish: transcode, tag, name and file a finished track (SPEC.md §14)."""

import logging
import sqlite3
from pathlib import Path

from music.publish import fields, naming, tag, transcode

log = logging.getLogger(__name__)

# track ids whose cover was replaced by the most recent retag pass, so the
# cli can say so instead of reporting a silent success.
_REFRESHED: set[int] = set()


def refreshed_artwork() -> set[int]:
  """Tracks whose embedded cover was replaced since the last reset.

  Returns:
    The track ids.
  """
  return set(_REFRESHED)


def reset_refreshed() -> None:
  """Clear the record, at the start of a retag pass."""
  _REFRESHED.clear()


__all__ = [
  "fetch_artwork",
  "fields",
  "layout_path",
  "naming",
  "prune_empty",
  "publish_track",
  "reject",
  "refreshed_artwork",
  "reset_refreshed",
  "retag",
  "sweep_empty",
  "tag",
  "transcode",
]


def layout_path(library: Path, artist: str, title: str) -> Path:
  """Build the output path.

  `library/<Artist> - <Title>.aiff` — one flat directory.

  Nesting was tried two ways and both lost to real data (SPEC.md §14). By
  *genre family*, the folder was a second job for a value that exists to select
  a precedence table, and correcting a genre meant moving the file. By
  *artist*, collaborations shatter a performer's catalogue: Arijit Singh
  occupies ten folders in a 120-track library, including both
  `Antara Mitra & Arijit Singh` and `Arijit Singh & Antara Mitra`.

  Flat also means the path depends on the two fields least likely to be wrong,
  so the resolver can keep improving without moving files around underneath
  rekordbox and serato, which track by path.

  Args:
    library: Library root.
    artist: Artist name.
    title: Track title.

  Returns:
    Full destination path.
  """
  return library / naming.filename(artist, title)


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


def retag(
  conn: sqlite3.Connection, track_id: int, library: Path | None = None
) -> Path | None:
  """Rewrite an already-published file's tags from the database.

  This is the payoff for treating the database as the source of truth: a better
  resolver re-tags the library without re-downloading or re-transcoding
  anything. Manual edits survive because arbitration never overwrote them
  (SPEC.md §15).

  The file is also renamed if canonical naming now produces a different name.

  Args:
    conn: Open connection.
    track_id: Track to re-tag.
    library: Library root; read from config when omitted.

  Returns:
    The file's path, or None if the track has not been published.
  """
  if library is None:
    from music import config

    library = config.load().paths.library
  row = conn.execute(
    "SELECT published_path, genre_family, artwork_url FROM track WHERE id = ?",
    (track_id,),
  ).fetchone()
  if row is None or not row["published_path"]:
    return None
  path = Path(row["published_path"])
  if not path.exists():
    log.warning("published file missing, cannot retag: %s", path)
    return None

  built = fields.build_for(conn, track_id)
  existing = tag.read(path)

  # Refresh the cover when the resolved artwork points somewhere new. Retag
  # used to keep whatever the file already had, unconditionally — so a cover
  # corrected in the review ui was written to the database, reported as saved,
  # and never reached the file. The url that produced the embedded image is
  # recorded on the track, which is the only way to tell a changed cover from
  # an unchanged one without re-downloading every image on every retag.
  wanted = fields.load_resolved(conn, track_id).get("artwork_url", "")
  if wanted and wanted != (row["artwork_url"] or ""):
    fetched = fetch_artwork(wanted)
    if fetched:
      built.tags.artwork = fetched
      conn.execute("UPDATE track SET artwork_url = ? WHERE id = ?", (wanted, track_id))
      _REFRESHED.add(track_id)
    else:
      log.warning("could not fetch new artwork for track %s", track_id)
  if existing.artwork and not built.tags.artwork:
    built.tags.artwork = existing.artwork
  tag.write(path, built.tags)

  renamed = _rename_if_needed(conn, track_id, path, library, built.tags)
  return renamed or path


def _rename_if_needed(
  conn: sqlite3.Connection,
  track_id: int,
  path: Path,
  library: Path,
  tags: tag.Tags,
) -> Path | None:
  """Move a published file if canonical naming now yields a different path.

  The library root is passed in rather than derived from the file's own path:
  a file still sitting in the old `<family>/<artist>/` tree is at a different
  depth from one already flat, so deriving it climbs the wrong number of
  levels. This is also what migrates the old layout — a retag relocates every
  file and sweeps up the directories it empties.
  """
  artist = tags.values.get("artist")
  title = tags.values.get("title")
  if not artist or not title:
    return None
  target = layout_path(library, artist, title)
  if target == path or target.exists():
    return None
  target.parent.mkdir(parents=True, exist_ok=True)
  path.replace(target)
  prune_empty(path.parent, library)
  conn.execute(
    "UPDATE track SET published_path = ?, updated_at = datetime('now') WHERE id = ?",
    (str(target), track_id),
  )
  return target


# Finder writes `.DS_Store` into every directory it displays, so a folder
# emptied of music is almost never empty on disk. Treating these as absent is
# what makes pruning work at all on macOS; they hold Finder view settings and
# nothing else.
IGNORABLE = frozenset({".DS_Store", ".localized"})


def _is_vacant(directory: Path) -> bool:
  """Whether a directory holds nothing but macOS metadata.

  Args:
    directory: Directory to inspect.

  Returns:
    True when it can be removed.
  """
  try:
    return all(entry.name in IGNORABLE for entry in directory.iterdir())
  except OSError:
    return False


def prune_empty(directory: Path, library: Path) -> int:
  """Remove directories left empty by a move, up to but never including root.

  Args:
    directory: The directory the file came from.
    library: Library root, which is never removed.

  Returns:
    How many directories were removed.
  """
  current = directory.resolve()
  root = library.resolve()
  removed = 0
  while current != root and root in current.parents and _is_vacant(current):
    parent = current.parent
    try:
      for entry in current.iterdir():
        entry.unlink()
      current.rmdir()
    except OSError:
      return removed
    removed += 1
    current = parent
  return removed


def sweep_empty(library: Path) -> int:
  """Remove every vacant directory under the library.

  A per-move prune only reaches the directory a file just left. Migrating a
  whole library leaves siblings behind, so a pass is swept at the end.

  Args:
    library: Library root, which is never removed.

  Returns:
    How many directories were removed.
  """
  if not library.is_dir():
    return 0
  removed = 0
  # deepest first, so a parent is considered only once its children are gone
  for directory in sorted(
    (p for p in library.rglob("*") if p.is_dir()),
    key=lambda p: len(p.parts),
    reverse=True,
  ):
    if directory.exists():
      removed += prune_empty(directory, library)
  return removed


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

  dest = layout_path(library, artist, title)
  # collisions are flagged rather than silently overwritten (SPEC.md §14).
  if dest.exists():
    dest = dest.with_name(f"{dest.stem} (2){dest.suffix}")

  work = staging / f"{track_id}.aiff"
  transcode.to_aiff(source, work)

  resolved = fields.load_resolved(conn, track_id)
  art_url = resolved.get("artwork_url", "")
  embedded_url = ""
  if art_url:
    tags.artwork = fetch_artwork(art_url)
    if tags.artwork:
      embedded_url = art_url
  if not tags.artwork:
    # fall back to whatever the downloaded file already carried
    art = transcode.extract_artwork(source, staging / f"{track_id}.jpg")
    if art:
      tags.artwork = art.read_bytes()
  tag.write(work, tags)

  dest.parent.mkdir(parents=True, exist_ok=True)
  work.replace(dest)
  conn.execute(
    "UPDATE track SET published_path=?, status='published', artwork_url=?,"
    " updated_at=datetime('now') WHERE id=?",
    (str(dest), embedded_url, track_id),
  )
  return dest


def reject(
  conn: sqlite3.Connection, track_id: int, library: Path | None = None
) -> dict[str, object]:
  """Delete a track's audio and remember that it was rejected.

  The `source_file` row is deliberately **kept**. `already_have` checks it
  before downloading, so the tombstone is what stops the track reappearing the
  next time its playlist is ingested — which is the whole point when a hundred
  arrive at once and a dozen are never going to be played.

  Both copies go: the published AIFF and the staging download. Reclaiming the
  space is the reason for doing this, and the tombstone means the decision is
  not lost with the bytes.

  Args:
    conn: Open connection.
    track_id: Track to reject.
    library: Library root, for pruning directories the file leaves empty.

  Returns:
    What was removed, for reporting.

  Raises:
    RuntimeError: If there is no such track.
  """
  row = conn.execute(
    "SELECT t.published_path, s.staging_path, s.video_id,"
    " (SELECT value FROM resolved_field WHERE track_id=t.id AND field='artist') artist,"
    " (SELECT value FROM resolved_field WHERE track_id=t.id AND field='title') title"
    " FROM track t JOIN source_file s ON s.id = t.source_file_id WHERE t.id = ?",
    (track_id,),
  ).fetchone()
  if row is None:
    raise RuntimeError(f"no track {track_id}")

  freed = 0
  removed: list[str] = []
  for kind in ("published_path", "staging_path"):
    raw = row[kind]
    if not raw:
      continue
    path = Path(raw)
    if not path.exists():
      continue
    freed += path.stat().st_size
    path.unlink()
    removed.append(kind.replace("_path", ""))
    if kind == "published_path" and library is not None:
      prune_empty(path.parent, library)

  # a preview encoded from the published file would otherwise be orphaned
  if library is not None:
    preview = Path(row["staging_path"]).parent / f"{track_id}.preview.m4a"
    if preview.exists():
      freed += preview.stat().st_size
      preview.unlink()

  conn.execute("DELETE FROM review_queue WHERE track_id = ?", (track_id,))
  conn.execute(
    "UPDATE track SET status = 'skipped', published_path = NULL,"
    " artwork_url = NULL, updated_at = datetime('now') WHERE id = ?",
    (track_id,),
  )
  label = (
    " - ".join(x for x in (row["artist"], row["title"]) if x) or f"track {track_id}"
  )
  log.info("rejected %s (%.1f MB reclaimed)", label, freed / 1024**2)
  return {
    "track_id": track_id,
    "label": label,
    "removed": removed,
    "freed_bytes": freed,
    "video_id": row["video_id"] or "",
  }
