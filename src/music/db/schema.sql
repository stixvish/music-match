-- schema for the music metadata pipeline. see SPEC.md §11.
-- the database is the source of truth; audio files are a regenerable projection.

PRAGMA foreign_keys = ON;

-- immutable record of one acquired audio file.
CREATE TABLE IF NOT EXISTS source_file (
  id            INTEGER PRIMARY KEY,
  origin        TEXT    NOT NULL CHECK (origin IN ('youtube', 'local')),
  video_id      TEXT    UNIQUE,
  local_path    TEXT,
  staging_path  TEXT    NOT NULL,
  sha256        TEXT    NOT NULL,
  duration_s    REAL    NOT NULL,
  codec         TEXT,
  bitrate       INTEGER,
  sample_rate   INTEGER,
  itag          TEXT,
  channel       TEXT,
  description   TEXT,
  raw_tags      TEXT,
  acquired_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- one logical track. `stage` is the resume marker (SPEC.md §13).
CREATE TABLE IF NOT EXISTS track (
  id                  INTEGER PRIMARY KEY,
  source_file_id      INTEGER NOT NULL REFERENCES source_file(id) ON DELETE CASCADE,
  stage               TEXT    NOT NULL DEFAULT 'acquired',
  status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','auto','review','published','failed','skipped')),
  genre_family        TEXT,
  genre_style         TEXT,
  is_video_rip        INTEGER NOT NULL DEFAULT 0,
  identity_confidence REAL,
  norm_artist         TEXT,
  norm_title          TEXT,
  chromaprint         TEXT,
  acoustid            TEXT,
  isrc                TEXT,
  mb_recording_id     TEXT,
  published_path      TEXT,
  error               TEXT,
  updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_track_stage ON track(stage, status);
CREATE INDEX IF NOT EXISTS idx_track_isrc ON track(isrc) WHERE isrc IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_track_acoustid ON track(acoustid) WHERE acoustid IS NOT NULL;

-- every value every source offered. append-only; arbitration reads it.
CREATE TABLE IF NOT EXISTS field_candidate (
  id         INTEGER PRIMARY KEY,
  track_id   INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE,
  field      TEXT    NOT NULL,
  value      TEXT,
  source     TEXT    NOT NULL,
  confidence REAL,
  fetched_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cand ON field_candidate(track_id, field);

-- arbitration output: what gets written to the file.
CREATE TABLE IF NOT EXISTS resolved_field (
  track_id   INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE,
  field      TEXT    NOT NULL,
  value      TEXT,
  source     TEXT,
  decided_by TEXT    NOT NULL
               CHECK (decided_by IN ('precedence','fallback','manual','url_override')),
  PRIMARY KEY (track_id, field)
);

-- the elicitation exercise fills this in; one row per cell.
CREATE TABLE IF NOT EXISTS precedence (
  genre_family TEXT    NOT NULL,
  field        TEXT    NOT NULL,
  rank         INTEGER NOT NULL,
  source       TEXT    NOT NULL,
  PRIMARY KEY (genre_family, field, rank)
);

CREATE TABLE IF NOT EXISTS review_queue (
  track_id    INTEGER PRIMARY KEY REFERENCES track(id) ON DELETE CASCADE,
  reason      TEXT NOT NULL
                CHECK (reason IN ('low_confidence','no_match','video_rip','duplicate')),
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at TEXT
);

-- persistent http cache. re-running resolution must not re-hit the network.
CREATE TABLE IF NOT EXISTS api_cache (
  key        TEXT PRIMARY KEY,
  source     TEXT NOT NULL,
  response   TEXT NOT NULL,
  fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
