"""The two dedup layers that run before anything is downloaded.

**Layer 1 — the archive check.** Every completed download records its
source id, so a link submitted twice is recognised instantly with no
network call at all. This is the same idea as yt-dlp's own
`--download-archive`, kept in SQLite so `reindex` can rebuild it from the
files themselves.

**Layer 2 — the metadata pre-check.** For anything new, compare the title
and duration the source reports against what the library already holds.
This one **never decides on its own**: a shared title is often a
legitimately different mix, and silently skipping a track you wanted is
worse than downloading one you already had. It reports candidates and
leaves the decision to a human.

Layer 3 is the audio fingerprint, which runs after download in the
tagging phase — it is the only layer that can compare the audio itself.
"""

import dataclasses
import pathlib
import sqlite3

from music_match.intake.entries import Entry
from music_match.matching import normalize as norm
from music_match.matching import score as scoring

# How close two durations must be for the pre-check to raise a candidate.
# Wider than the fingerprint dedup's window because this is comparing a
# source's rounded duration against a decoded file's exact one, and
# because a false candidate here only costs a question.
DURATION_TOLERANCE_SECONDS = 12.0

# How similar two titles must look. Deliberately not high: this is a
# prompt to look, not a verdict.
TITLE_SIMILARITY = 0.65


@dataclasses.dataclass(frozen=True)
class Candidate:
  """An existing library file that a new entry might duplicate.

  Attributes:
    path: Where the existing file is.
    title: The title it was compared on.
    duration_seconds: Its duration, if known.
    similarity: How closely the titles matched, 0.0 to 1.0.
  """
  path: pathlib.Path
  title: str
  duration_seconds: float | None
  similarity: float

  def describe(self) -> str:
    """Returns a one-line summary for a confirmation prompt."""
    length = ("unknown length" if self.duration_seconds is None else
              f"{int(self.duration_seconds // 60)}:"
              f"{int(self.duration_seconds % 60):02d}")
    return f"{self.path.name} ({length}, {self.similarity:.0%} title match)"


def _title_similarity(entry: Entry, path: pathlib.Path) -> float:
  """Compares an entry's title against a library file name.

  The two sides are not shaped alike: a source title may be just the
  song, or "Artist - Song", while the file is named "Uploader - Song".
  Comparing a bare title against a full file name scores low even when
  they are the same track, so both framings are tried and the better one
  wins.

  Args:
    entry: The entry being considered.
    path: An existing library file.

  Returns:
    The best similarity found, 0.0 to 1.0.
  """
  _, file_title = norm.split_filename(path.stem)
  return max(
      scoring.similarity(entry.label(), path.stem),
      scoring.similarity(entry.title, file_title),
  )


def in_archive(connection: sqlite3.Connection, entry: Entry) -> bool:
  """Returns whether this entry has already been downloaded.

  Args:
    connection: An open connection.
    entry: The entry to check.

  Returns:
    True if its source id is in the archive.
  """
  row = connection.execute(
      "SELECT 1 FROM download_archive WHERE extractor = ? AND video_id = ?",
      (entry.extractor, entry.video_id)).fetchone()
  return row is not None


def record_download(connection: sqlite3.Connection,
                    entry: Entry,
                    track_id: int | None = None) -> None:
  """Records a completed download so it is never fetched again.

  Args:
    connection: An open connection.
    entry: The entry that was downloaded.
    track_id: The indexed track it produced, if known.
  """
  connection.execute(
      "INSERT INTO download_archive (extractor, video_id, track_id)"
      " VALUES (?, ?, ?)"
      " ON CONFLICT(extractor, video_id) DO UPDATE SET"
      "   track_id = coalesce(excluded.track_id, download_archive.track_id)",
      (entry.extractor, entry.video_id, track_id))


def find_candidates(connection: sqlite3.Connection,
                    entry: Entry,
                    *,
                    tolerance: float = DURATION_TOLERANCE_SECONDS,
                    threshold: float = TITLE_SIMILARITY) -> list[Candidate]:
  """Finds library files that this entry might already duplicate.

  Compares against the file name rather than its tags: a fresh download
  is often untagged, and the name is what both sides reliably have.

  Args:
    connection: An open connection.
    entry: The entry being considered.
    tolerance: Seconds two durations may differ by.
    threshold: Minimum title similarity to raise a candidate.

  Returns:
    Possible duplicates, closest first. Empty when nothing looks alike.
  """
  if not entry.title:
    return []
  rows = connection.execute(
      "SELECT path, duration_seconds FROM tracks").fetchall()
  candidates = []
  for row in rows:
    path = pathlib.Path(row["path"])
    duration = row["duration_seconds"]
    if (entry.duration_seconds and duration and
        abs(entry.duration_seconds - duration) > tolerance):
      continue
    similarity = _title_similarity(entry, path)
    if similarity >= threshold:
      candidates.append(
          Candidate(path=path,
                    title=path.stem,
                    duration_seconds=duration,
                    similarity=similarity))
  candidates.sort(key=lambda item: -item.similarity)
  return candidates
