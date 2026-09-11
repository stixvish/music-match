# Music Metadata Pipeline — Specification

**Status:** format probe COMPLETE · AIFF confirmed · no open probe items
**Last updated:** 2026-09-11

## 1. Problem

~2,300 tracks sourced from YouTube via yt-dlp, in M4A/AAC. The container cannot
hold the metadata fields required for DJ use, and identity/credits are
incomplete. Rekordbox 7 and Serato DJ Lite must both display a complete,
correct tag set.

## 2. Goals

- Every track carries a complete tag set in **Rekordbox**, and as complete a set
  as **Serato DJ Lite** can display (it has no columns for album artist, mix
  name, original artist, lyricist, disc, or ISRC — see §10).
- Point at a YouTube playlist and get a tagged library out.
- Point at a local file (or a Spotify/MusicBrainz/Discogs URL) and get the same.
- Metadata is regenerable: improving the resolver re-tags the library for free.
- Manual review stays within a **5-hour total budget** (§9).

## 3. Non-goals (v1)

- Tidal, Bandcamp, Apple Music API — gated or paid. Deferred to v2.
- Cue points, beatgrids, crates — owned by the DJ apps, not this tool.
- **Playlist provenance** — playlists will be rebuilt by hand in Rekordbox.
- **`tutorial-tracks/`** — excluded from the pipeline entirely.
- **Migrating the existing 2,329 M4A files** — they are being re-downloaded.
  Their tags are deliberately discarded (§4, Option C).
- 100% composer/lyricist coverage — best-effort, never blocking.

## 4. Verified findings (evidence base)

| Finding | Evidence |
|---|---|
| Library is 2,329 files, 100% M4A/AAC, 16 GB | full ffprobe scan |
| 98.5% already at 255–256 kbps (YouTube's max, itag 141) | audio-stream bitrate scan |
| 75.4% carry ISRC; 72.2% complete on core 8 fields | tag coverage scan |
| M4A cannot hold remixer/label/original artist/mix name | Pioneer metadata spec |
| 256 kbps reachable via `web_music` + Premium cookies | downloaded & verified 256012 bps |
| itag 141 (AAC, 44.1 kHz) preferred over 774 (Opus, 48 kHz + 20 kHz lowpass) | format listing |
| 117 files are music-video rips (channel name as artist, inflated duration) | filename/tag pattern scan |
| Library is 85% non-electronic (hip-hop/pop/R&B/Bollywood) | genre distribution |
| essentia-tensorflow works on Python 3.14.7; emits Discogs taxonomy | installed, ran on library files |
| FLAC 16-bit = 57.7 GB vs AIFF 79.4 GB | measured on probe files |
| **Rekordbox reads ALL required fields from AIFF/ID3v2.4** | format probe, Rekordbox 7 |
| **Rekordbox FLAC drops album artist, release date, original artist, mix name, lyricist** | format probe; 3–4 uppercase key spellings tried per field |
| Lyricist DOES round-trip via ID3 `TEXT` (contra forum reports) | format probe |
| Comment key differs by app in FLAC: Rekordbox `COMMENT`, Serato `DESCRIPTION` | format probe |
| **A fresh yt-dlp download yields NO ISRC, album artist, track, disc, composer, lyricist or key; genre is literally `"Music"`** | downloaded an art track with `--embed-metadata` |
| **Query hygiene is worth +31pp of resolution accuracy** (24% → 55% high-confidence) | 30-track MusicBrainz sample, before/after cleaning |
| Un-cleaned failures are VEVO channel names and `Artist - ` title prefixes | same sample |
| Residual ambiguity is duration mismatch on music-video rips, not identity doubt | same sample (`rivals=0, dur_ok=False`) |
| MusicBrainz misses remixes/edits (`(LEFTI REMIX)`, `- H.K.G Mix`) | same sample |
| Text match can confidently return the wrong *version* (`SMASH!` → `SMASH! (instrumental)`) | same sample |

## 5. Architecture

Source of truth is **SQLite**. Audio files are a regenerable *projection* of it.
Re-tagging never requires re-downloading or re-resolving.

```
music ingest <playlist-url>     music add <file|dir>     music add --url <link>
        │                              │                          │
        ├─ enumerate                   │                          │
        ├─ dedup check ← PRE-download  │                          │
        ├─ download (141/774/140/251)  │                          │
        ├─ quality gate (<256k = fail) │                          │
        └───────────────┬──────────────┴──────────────────────────┘
                        ▼
      probe → normalise → classify → resolve → arbitrate
                        ▼
           transcode → AIFF · write ID3v2.4 · publish
```

Three entry points, one pipeline. `music add` (local file) is what converts the
7 purchased Beatport WAVs and the SoundCloud track; it is the ingest path minus
the download stage. `music add --url` supplies an authoritative identity
directly and skips resolution.

Every stage is **resumable**: each track's stage marker is a DB row, so a crash
at track 1,800 of 2,329 resumes at 1,800.

## 6. Pipeline stages

1. **Enumerate** — playlist → video IDs + title/channel/duration.
2. **Dedup** — skip anything already owned (§8). Runs pre-download.
3. **Download** — yt-dlp Python API (not subprocess: structured info dict).
   Format chain `141/774/140/251`; `web_music` client; cookies from file.
4. **Quality gate** — hard-fail below 256 kbps rather than silently degrade.
5. **Normalise** — clean artist/title before any query. Strip `VEVO` suffixes,
   a redundant leading `Artist - `, `(Official Video)` / `(Lyric Video)` /
   `(Visualizer)`, and trailing `feat.` clauses; take the primary artist only.
   **Measured worth +31pp of resolution accuracy (§4) — the highest-leverage
   stage in the pipeline.**
6. **Classify** — Essentia `genre_discogs400` → `Genre---Style`. Runs *before*
   resolution; its output routes precedence (§7).
7. **Resolve identity** — AcoustID fingerprint → normalised text search →
   manual URL override. Emits a confidence score (§12). Penalise version
   variants (`(instrumental)`, `(sped up)`) the query did not ask for.
8. **Arbitrate** — per-(genre-family → field) precedence (§12).
9. **Transcode** — ffmpeg → **AIFF** (`-c:a pcm_s16be`), native sample rate
   (no resampling). 16-bit is correct: the source is lossy AAC.
10. **Tag** — ID3v2.4 frames (§10).
11. **Publish** — write to the layout in §14, record provenance. Tracks below
    the confidence threshold are **not published**; they go to the review
    queue (§9) and stay out of the library until resolved.

## 7. Sources and precedence

**v1 sources (all free, no paid signup):** MusicBrainz, AcoustID, Discogs,
Spotify, iTunes Search API, local Essentia classifier. Beatport behind a
pluggable adapter.

Every source implements one interface:

```
lookup(identity) -> [FieldCandidate(field, value, source, confidence)]
```

Arbitration never knows which source it is talking to. A broken source returns
an empty list; the pipeline continues. This is why Beatport — the only source
with no official API, and the one most likely to break — is isolated behind it.

**Genre selects the table; the table ranks sources per field.**

```
family = classify(audio)                 # local, source-independent, pre-API
for field in FIELDS:
    ranked = PRECEDENCE[family][field]   # e.g. ["discogs","musicbrainz","spotify"]
    value  = first source in `ranked` holding a candidate
```

Genre family comes from the *local* classifier, not from a source. This breaks
the circular dependency where you would need genre to pick the precedence
table, but genre is itself one of the contested fields.

**Six genre families**, taken from the Discogs top-level genre (the part before
`---` in the classifier's `Genre---Style` output) and collapsed:

`electronic` · `hip-hop` · `pop` · `r&b-soul` · `world` · `other`

> **Risk:** Discogs files Indian film music inconsistently under
> *Folk, World, & Country* or *Stage & Screen*. The ~105 Bollywood tracks are
> where family routing is least reliable **and** source coverage is weakest.
> Spot-check before trusting `world` precedence.

| Field | Leading sources (to be calibrated, §9) |
|---|---|
| artist / title / mix name | Beatport (electronic) → Discogs → MusicBrainz → Spotify |
| featured vs. collaborating artists | **MusicBrainz** (models artist-credit; Spotify flattens) |
| genre / style | Essentia + Discogs Style → Beatport |
| label / catalog no. | Discogs → Beatport |
| release date | MusicBrainz release-group (original, not reissue) → Spotify |
| artwork | iTunes Search API |
| ISRC | existing tag → Spotify → MusicBrainz |
| BPM | local (Essentia). **Rekordbox overwrites `TBPM` with its own analysis; only Serato honours the tag** (§10) |
| key | local (Essentia). Honoured by *both* apps, and Serato DJ Lite cannot detect key at all — so `TKEY` is load-bearing |

**Rate limiting and caching** (§13) are part of this layer, not an afterthought:
every source adapter backs off exponentially and every response is cached to
disk, because resolution will be re-run many times as precedence is tuned.

## 8. Identity and deduplication

Stored per file: `video_id`, `isrc`, `chromaprint`, `acoustid_id`,
`musicbrainz_recording_id`, `sha256`, `duration`, normalized `artist+title`.

Chromaprint fingerprints are fuzzy and are **not** compared locally. They are
submitted to AcoustID, which returns a stable ID; equality on that ID is the
dedup test. Fuzzy matching becomes an exact key lookup.

> **Rule:** duplicate if any strong key matches **and** durations are within
> ~2 s. Never on title alone.

The duration tolerance is what keeps Extended Mix distinct from Radio Edit, and
an original distinct from its remix — required, since "identical copies get
thrown out, versions do not."

On a duplicate hit: prefer higher bitrate; if equal (the common case once
everything is itag 141), prefer the **art-track** source over a video rip; if
still tied, keep the incumbent. Always log the decision.

**Music-video detection** (duration alone is insufficient):

| Signal | Meaning |
|---|---|
| channel is `X - Topic` | art track — clean audio (strongest positive) |
| `Provided to YouTube by...` in description | label-delivered audio |
| title matches `Official (Music )?Video\|M/V\|Visualizer\|Lyric Video` | video rip |
| `artist` tag equals channel name | video rip |
| duration vs. catalog duration > ~5 s | intro/outro/skit |

Prefer art tracks at *search* time; detection is the safety net.

## 9. Review budget — 5 hours total

| Activity | Budget | Rate | Volume |
|---|---|---|---|
| Precedence elicitation (one-time) | 90 min | ~45 s | ~120 tracks |
| Review queue | 210 min | ~35 s | ~360 items |

Requires **≥84% auto-accept**. Under Option C there are no ISRCs to lean on, so
this is projected from the measured no-ISRC path:

```
MusicBrainz text, cleaned queries            55% high-confidence   (measured, n=29)
  + clean art-track audio (fixes the
    duration-mismatch AMBIG bucket)        → ~75-80%               (projected)
  + AcoustID, Spotify, iTunes, Discogs     → ~85%                  (projected)
                                           → review ≈ 350 ≈ 3.4 h  ✓
```

**This only works with the normalisation stage (§6.5).** At 24% — the
un-cleaned rate — review would be ~8 hours and the budget would be blown.

**Manual URL override is a budget lever**, not just a convenience: pasting a
Spotify/Discogs link resolves a track exactly, turning the hardest review items
(remixes MusicBrainz cannot find) from ~90-second puzzles into ~10 seconds.

**The review queue triggers on identity uncertainty, not field disagreement.**
If the recording is known, field conflicts resolve silently via precedence.
Genre disagreements never enter the queue — genre is subjective and would
consume the entire budget.

Expected outcome: **98–99% correct**, ~25–35 tracks with one wrong field.
Reaching 99.9% would need 20+ hours. The cheaper lever is re-running resolution
later against the DB.

**Elicitation method:** stratified sample across genre families; for each
disputed field, present candidate values side by side with source order
randomized; derive per-(field × family) weights from the choices.

## 10. Format probe — results

**Outcome: AIFF, not FLAC.** Rekordbox 7 renders every required field from
AIFF/ID3v2.4. FLAC silently drops five, two of which (album artist, release
date) are on the must-have list. Keys were verified uppercase on disk, and 3–4
spellings were tried per failing field, so this is reader coverage, not a
key-naming mistake. AIFF also plays on every CDJ generation; FLAC requires
NXS2 or newer. Cost of the reversal: 79.4 GB instead of 57.7 GB.

### Confirmed ID3v2.4 frame map (Rekordbox 7)

| Field | Frame | Field | Frame |
|---|---|---|---|
| title | `TIT2` | composer | `TCOM` |
| artist | `TPE1` | lyricist | `TEXT` |
| album | `TALB` | remixer | `TPE4` |
| album artist | `TPE2` | mix name | `TIT3` |
| genre | `TCON` | label | `TPUB` |
| track no. | `TRCK` | original artist | `TOPE` |
| disc no. | `TPOS` | key | `TKEY` |
| BPM | `TBPM` | ISRC | `TSRC` |

Each field was written with exactly one frame, so display confirms the mapping.

### Confirmed by screenshot

- `TDRC` → Rekordbox **Year**; `TDRL` → Rekordbox **Release Date**. Write both.
- Artwork (`APIC`) renders in Rekordbox. AIFF bitrate 1,411.2 kbps = correct
  16-bit/44.1/stereo.
- **BPM is not authoritative in Rekordbox** — it analyses and overwrites `TBPM`
  (wrote 121, displayed 128.00). Serato *does* honour the tag. Still write it:
  it is load-bearing for Serato only.
- **`TKEY` IS honoured by Rekordbox** (displayed our `1A` sentinel). Since
  Serato DJ Lite has no key detection, `TKEY` is load-bearing in both apps.
- **Serato DJ Lite has no columns** for album artist, mix name, original
  artist, lyricist, disc number, or ISRC. These are display limits, not format
  failures — write them anyway; they reach Serato Pro and CDJ export.
- Comment via `COMM` renders in **both** apps. It was invisible in Rekordbox
  until **Reload Tag** — confirming that Rekordbox caches tags per path and
  will not re-read on reimport. Operational rule: **tag before import.**

### FLAC key map (recorded for a possible space-constrained USB build)

`REMIXER` (not MIXARTIST) · `LABEL` · `INITIALKEY` (not KEY) · `BPM` (not
TEMPO) · `DATE` · `GROUPING` · comment: Rekordbox reads `COMMENT`, Serato reads
`DESCRIPTION` — write both.

### Capacity note

AIFF library ≈ 79.4 GB against a 64 GB USB: ~80% fits. Accepted. Fallback if it
bites: AIFF master library, FLAC/320k subset generated for the stick.

## 11. Data model

SQLite. Audio files are a **projection** of this; re-tagging never requires
re-downloading or re-resolving.

```sql
-- Immutable record of one acquired audio file.
CREATE TABLE source_file (
  id            INTEGER PRIMARY KEY,
  origin        TEXT NOT NULL,          -- 'youtube' | 'local'
  video_id      TEXT UNIQUE,            -- youtube only; pre-download dedup key
  local_path    TEXT,                   -- local ingest only
  staging_path  TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  duration_s    REAL NOT NULL,
  codec TEXT, bitrate INTEGER, sample_rate INTEGER, itag TEXT,
  channel       TEXT,                   -- '- Topic' => art track
  description   TEXT,                   -- 'Provided to YouTube by' => art track
  raw_tags      TEXT,                   -- JSON, as acquired
  acquired_at   TEXT NOT NULL
);

-- One logical track. Carries pipeline state; `stage` is the resume marker.
CREATE TABLE track (
  id                  INTEGER PRIMARY KEY,
  source_file_id      INTEGER NOT NULL REFERENCES source_file(id),
  stage               TEXT NOT NULL,    -- normalise|classify|resolve|arbitrate|transcode|tag|publish
  status              TEXT NOT NULL,    -- pending|auto|review|published|failed|skipped
  genre_family        TEXT,             -- routes precedence
  genre_style         TEXT,             -- full 'Genre---Style'
  is_video_rip        INTEGER DEFAULT 0,
  identity_confidence REAL,
  norm_artist TEXT, norm_title TEXT,    -- post-normalisation, what we query with
  chromaprint TEXT, acoustid TEXT, isrc TEXT, mb_recording_id TEXT,
  published_path      TEXT,
  updated_at          TEXT NOT NULL
);

-- Every value every source offered. Never overwritten; arbitration reads it.
CREATE TABLE field_candidate (
  id          INTEGER PRIMARY KEY,
  track_id    INTEGER NOT NULL REFERENCES track(id),
  field       TEXT NOT NULL,
  value       TEXT,
  source      TEXT NOT NULL,            -- musicbrainz|discogs|spotify|itunes|essentia|beatport|manual
  confidence  REAL,
  fetched_at  TEXT NOT NULL
);
CREATE INDEX idx_cand ON field_candidate(track_id, field);

-- Arbitration output: what actually gets written to the file.
CREATE TABLE resolved_field (
  track_id   INTEGER NOT NULL REFERENCES track(id),
  field      TEXT NOT NULL,
  value      TEXT,
  source     TEXT,
  decided_by TEXT NOT NULL,             -- precedence|manual|url_override
  PRIMARY KEY (track_id, field)
);

-- The elicitation exercise fills this in; one row per cell.
CREATE TABLE precedence (
  genre_family TEXT NOT NULL, field TEXT NOT NULL,
  rank INTEGER NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (genre_family, field, rank)
);

CREATE TABLE review_queue (
  track_id   INTEGER PRIMARY KEY REFERENCES track(id),
  reason     TEXT NOT NULL,             -- low_confidence|no_match|video_rip|duplicate
  created_at TEXT NOT NULL, resolved_at TEXT
);

-- Persistent HTTP cache. Re-running resolution must not re-hit the network.
CREATE TABLE api_cache (
  key TEXT PRIMARY KEY, source TEXT NOT NULL,
  response TEXT NOT NULL, fetched_at TEXT NOT NULL
);
```

`field_candidate` is append-only on purpose: when a tag looks wrong six months
from now, the full set of what every source said is still there to inspect.

## 12. Confidence and arbitration

**Identity confidence gates entry, not ranking.** One threshold, one ordered
list. If a field is wrong you can point at exactly which source supplied it.

| Identity evidence | Confidence |
|---|---|
| Manual URL override | 1.00 |
| ISRC exact match | 0.98 |
| AcoustID ≥ 0.90 **and** duration within 3 s | 0.95 |
| Text match: score ≥ 90, duration within 3 s, no rival within 2 pts | 0.85 |
| Text match, duration mismatch **or** a close rival | 0.50 |
| No match | 0.00 |

- **≥ 0.80** → auto-accept. Candidates enter arbitration; precedence decides.
- **< 0.80** → nothing is written. Track goes to `review_queue` and is **not
  published**. An unresolved track never silently enters the library.

Version-variant guard: penalise a candidate whose title adds `(instrumental)`,
`(sped up)`, `(slowed)`, `(live)`, or `(radio edit)` when the query did not ask
for it. Measured need — a text match returned `SMASH! (instrumental)` at full
confidence with matching duration (§4).

## 13. Operational concerns

**Credentials.** This repository is public. There is **no credential file**:
yt-dlp reads cookies from Chrome at runtime via `--cookies-from-browser`. A
`cookies.txt` would hold live Google session tokens — full account access, not
just YouTube — and `.gitignore` is inadequate protection against `git add -f`.
Runtime config lives at `~/.config/musicpipeline/config.toml`, **outside the
repo**; only `config.example.toml` is committed. A pre-commit secret scan guards
the rest.

**Rate limiting.** MusicBrainz 1 req/s (and it 503s rather than queuing — 9 of
30 requests failed before backoff was added), Discogs 60/min authenticated on a
rolling window, AcoustID 3/s. Every adapter: exponential backoff with jitter,
and a descriptive User-Agent (MusicBrainz blocks requests without one).

**Caching.** Every response lands in `api_cache`. Resolution will be re-run many
times while precedence is tuned; reruns must cost nothing.

**Resumability.** `track.stage` is the marker. A crash at track 1,800 of 2,329
resumes at 1,800.

**Rekordbox caches tags per path and will not re-read on reimport** (§10).
Operational rule: **tag before import.** Never retag a file already in the
collection without a Reload Tag.

**Originals.** Staging files are retained until a track reaches `published` and
is verified, then eligible for cleanup. The 16 GB of existing M4A files stay
untouched until the new library is verified end to end.

**Dependencies.** `yt-dlp` (needs a JS runtime — Deno — for some formats),
`ffmpeg`, `essentia-tensorflow` (cp314 wheels), `mutagen`, `chromaprint/fpcalc`.

## 14. Output layout

```
Library/
  <genre-family>/
    <Artist>/
      <Artist> - <Title> (<Mix>).aiff
```

Six families (§7). Filename keeps the mix/remix designation — losing it is
worse than having no tag, since that is how a DJ searches. Album-based
foldering is rejected: Bollywood tracks sit on soundtrack "albums" that do not
match how they would be browsed, and the same holds for compilations.

Genre-first foldering also makes USB subsetting a folder copy, which matters at
79.4 GB against a 64 GB stick (§10).

## 15. Milestones

1. **M0** — format probe. ✅ **Done.** AIFF selected; frame map confirmed.
2. **M1** — schema (§11) + resumable stage runner.
3. **M2** — download stage: enumerate, pre-download dedup, quality gate.
4. **M3** — normalisation (§6.5) + resolution + confidence (§12).
   *Normalisation lands first; it is worth more than any source.*
5. **M4** — Essentia classification + arbitration.
6. **M5** — elicitation UI → `precedence` table.
7. **M6** — transcode + tag + publish (§14).
8. **M7** — full run over the playlists; review queue with URL override.
9. **M8** — `music add` for the 7 Beatport WAVs and the SoundCloud track.
