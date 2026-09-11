# Music Metadata Pipeline — Specification

**Status:** format probe COMPLETE · AIFF confirmed · no open probe items
**Last updated:** 2026-09-10

## 1. Problem

~2,300 tracks sourced from YouTube via yt-dlp, in M4A/AAC. The container cannot
hold the metadata fields required for DJ use, and identity/credits are
incomplete. Rekordbox 7 and Serato DJ Lite must both display a complete,
correct tag set.

## 2. Goals

- Every track carries a complete, verified tag set in both Rekordbox and Serato.
- One pipeline serves both backfill (existing files) and ingest (new downloads).
- Metadata is regenerable: improving the resolver re-tags the library for free.
- Manual review stays within a **5-hour total budget** (§9).

## 3. Non-goals (v1)

- Tidal, Bandcamp, Apple Music API — gated or paid. Deferred to v2.
- Cue points, beatgrids, playlists — owned by the DJ apps, not this tool.
- Library-wide dedup sweep — current duplicate rate is ~1% (28 files).
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

## 5. Architecture

Source of truth is **SQLite**. Audio files are a regenerable *projection* of it.
Re-tagging never requires re-downloading or re-resolving.

```
music ingest <playlist-url>          music backfill <dir>
        │                                     │
        ├─ enumerate (yt-dlp --flat-playlist) │
        ├─ dedup check  ← BEFORE download     │
        ├─ download (f=141/774/140/251)       │
        ├─ quality gate (fail loud < 256k)    │
        └──────────────┬──────────────────────┘
                       ▼
              probe · classify · resolve · arbitrate
                       ▼
              transcode → FLAC · write tags · publish
```

Every stage is **resumable**: each track's stage marker is a DB row, so a crash
at track 1,800 of 2,329 resumes at 1,800.

## 6. Pipeline stages

1. **Enumerate** — playlist → video IDs + title/channel/duration.
2. **Dedup** — skip anything already owned (§8). Runs pre-download.
3. **Download** — yt-dlp Python API (not subprocess: structured info dict).
   Format chain `141/774/140/251`; `web_music` client; cookies from file.
4. **Quality gate** — hard-fail below 256 kbps rather than silently degrade.
5. **Classify** — Essentia `genre_discogs400` → `Genre---Style`. Runs *before*
   resolution; its output routes precedence (§7).
6. **Resolve identity** — ISRC exact lookup → AcoustID fingerprint → text search.
   Emits a confidence score.
7. **Arbitrate** — per-(field × genre-family) precedence over source candidates.
8. **Transcode** — ffmpeg → **AIFF** (`-c:a pcm_s16be`), native sample rate
   (no resampling). 16-bit is correct: the source is lossy AAC.
9. **Tag** — ID3v2.4 frames (§10). Write every known alias for a field rather
   than picking one "correct" key; apps disagree (Rekordbox vs Serato comment).
10. **Publish** — move to library, record provenance (itag, sources, confidence).

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

**Precedence is per-(field × genre-family), conditioned on match confidence.**

Genre family comes from the *local* classifier, not from a source. This breaks
the circular dependency where you'd need genre to pick the precedence table,
but genre is itself a contested field.

| Field | Leading sources (to be calibrated, §9) |
|---|---|
| artist / title / mix name | Beatport (electronic) → Discogs → MusicBrainz → Spotify |
| featured vs. collaborating artists | **MusicBrainz** (models artist-credit; Spotify flattens) |
| genre / style | Essentia + Discogs Style → Beatport |
| label / catalog no. | Discogs → Beatport |
| release date | MusicBrainz release-group (original, not reissue) → Spotify |
| artwork | iTunes Search API |
| ISRC | existing tag → Spotify → MusicBrainz |
| BPM / key | local (Essentia) — Serato DJ Lite cannot detect key |

**Existing tags are a ranked source, not ground truth and not garbage.** The
1,755 existing ISRCs are the highest-value matching key in the library.

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

On a duplicate hit, keep the higher-bitrate file and log the decision.

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

Requires **≥84% auto-accept**. Projected from measured coverage:

```
1,755 with ISRC   → exact lookup        → auto-accept (<1% error)
  574 without     → AcoustID ~65% hit   → ~373 auto / ~201 review
  117 video rips  (overlapping)         → review
                                        → review ≈ 250–300  ✓
```

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

## 11. Milestones

1. **M0** — ~~format probe~~ **done**; AIFF selected. Two clarifications open (§10).
2. **M1** — DB schema + resumable stage runner.
3. **M2** — download stage with quality gate; re-pull 117 video rips + 31 low-bitrate.
4. **M3** — resolution (ISRC → AcoustID → text) + confidence scoring.
5. **M4** — Essentia classification + arbitration engine.
6. **M5** — elicitation UI → precedence weights.
7. **M6** — transcode + tag + publish.
8. **M7** — full backfill run; review queue.
