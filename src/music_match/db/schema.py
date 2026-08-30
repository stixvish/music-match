"""The SQLite schema.

One database holds everything the tool needs to remember between runs:
what files exist and what state they're in, every tag change ever made,
which source video IDs have already been downloaded, and which tracks
have been marked as never going to match.

Album art is deliberately *not* stored here. Covers are content-addressed
files under `.music-match/art-store/<sha256>.jpg`; a `tag_history` row for
`field = 'album_art'` holds the hash, not the bytes.

The `matched_*` columns hold the *proposed* metadata, not the file's
current tags: matching decides what should be written, and step 6 decides
whether and when to write it. Keeping the proposal separate is what lets a
low-confidence match sit in a review queue without touching the file.

`genre_confidence` sits beside `detected_genre` because the label on its
own is not worth much: measured against known tracks, the model's
top-level genre is right about 22% of the time below 0.15 confidence and
about 91% above 0.40. Anything deciding what to do with a detected genre
needs both numbers. Storing 640x640 JPEGs
as BLOBs across 2000 tracks and multiple revisions each would bloat the
database and slow every query that touches the table, including ones with
nothing to do with art.
"""

SCHEMA_VERSION = 3

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
  genre_confidence REAL,
  matched_source   TEXT,
  match_confidence REAL,
  matched_tags_json TEXT,
  matched_art_url  TEXT,
  matched_at       TEXT,
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

# Applied in order to bring an existing database up to SCHEMA_VERSION.
# Keyed by the version being upgraded *from*. A database created fresh
# already has every column, so these only run on one that predates them.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: ("ALTER TABLE tracks ADD COLUMN genre_confidence REAL",),
    2: (
        "ALTER TABLE tracks ADD COLUMN matched_source TEXT",
        "ALTER TABLE tracks ADD COLUMN match_confidence REAL",
        "ALTER TABLE tracks ADD COLUMN matched_tags_json TEXT",
        "ALTER TABLE tracks ADD COLUMN matched_art_url TEXT",
        "ALTER TABLE tracks ADD COLUMN matched_at TEXT",
    ),
}

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
