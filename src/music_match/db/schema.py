"""The SQLite schema.

One database holds everything the tool needs to remember between runs:
what files exist and what state they're in, every tag change ever made,
which source video IDs have already been downloaded, and which tracks
have been marked as never going to match.

Album art is deliberately *not* stored here. Covers are content-addressed
files under `.music-match/art-store/<sha256>.jpg`; a `tag_history` row for
`field = 'album_art'` holds the hash, not the bytes. Storing 640x640 JPEGs
as BLOBs across 2000 tracks and multiple revisions each would bloat the
database and slow every query that touches the table, including ones with
nothing to do with art.
"""

SCHEMA_VERSION = 1

TABLES = ("tracks", "tag_history", "download_archive", "wont_match")

MATCH_STATUSES = (
    "pending",
    "matched",
    "review",
    "no_match",
    "quarantined",
    "wont_match",
)

_TRACKS = """
CREATE TABLE IF NOT EXISTS tracks (
  id               INTEGER PRIMARY KEY,
  path             TEXT    NOT NULL UNIQUE,
  source_name      TEXT    NOT NULL,
  fingerprint      TEXT,
  duration_seconds REAL,
  detected_genre   TEXT,
  source_video_id  TEXT,
  tags_json        TEXT,
  match_status     TEXT    NOT NULL DEFAULT 'pending'
                     CHECK (match_status IN (
                       'pending', 'matched', 'review',
                       'no_match', 'quarantined', 'wont_match')),
  first_seen_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
)
"""

_TAG_HISTORY = """
CREATE TABLE IF NOT EXISTS tag_history (
  id         INTEGER PRIMARY KEY,
  track_id   INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  field      TEXT    NOT NULL,
  old_value  TEXT,
  new_value  TEXT,
  changed_at TEXT    NOT NULL DEFAULT (datetime('now'))
)
"""

_DOWNLOAD_ARCHIVE = """
CREATE TABLE IF NOT EXISTS download_archive (
  extractor      TEXT NOT NULL DEFAULT 'youtube',
  video_id       TEXT NOT NULL,
  track_id       INTEGER REFERENCES tracks(id) ON DELETE SET NULL,
  downloaded_at  TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (extractor, video_id)
)
"""

_WONT_MATCH = """
CREATE TABLE IF NOT EXISTS wont_match (
  track_id  INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
  reason    TEXT,
  marked_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

SCHEMA_STATEMENTS = (
    _TRACKS,
    _TAG_HISTORY,
    _DOWNLOAD_ARCHIVE,
    _WONT_MATCH,
    "CREATE INDEX IF NOT EXISTS idx_tracks_fingerprint"
    "  ON tracks(fingerprint) WHERE fingerprint IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_tracks_match_status"
    "  ON tracks(match_status)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_source_name"
    "  ON tracks(source_name)",
    "CREATE INDEX IF NOT EXISTS idx_tag_history_track"
    "  ON tag_history(track_id, changed_at)",
)
