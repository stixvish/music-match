# music-match — Architecture

## Goal

Automatically tag a ~2000-track library (growing) with accurate metadata
sourced from public music databases, so files work cleanly in Rekordbox.
Also handles fresh downloads end-to-end: paste links → download → tag.

## Source folders

| Folder | Format | Volume | Existing tags |
|---|---|---|---|
| `~/Music/yt-dlp` | M4A | ~1990 tracks | Often missing/wrong; genre usually just "Music" |
| `~/Music/beatport` | WAV | ~8 tracks | Generally decent |

Folders are matched by **name** (`yt-dlp`, `beatport`), not absolute path,
so the library can move or live on a different drive. New source folders
are added via config, never hardcoded.

Nothing outside the configured source folders is ever touched.

## Target metadata fields

**Rekordbox-visible:** track title, artist, original artist, composer,
lyricist, remixer, mix name, album, disc number, track number, album
artist, year, release date.

**Also required:** ISRC, genre, embedded album art (640×640).

**Internal bookkeeping (not user-facing):** source video ID, embedded at
download time in a non-Rekordbox tag field. Lets `reindex` rebuild the
download-archive log from files alone.

---

## Pipeline overview

Three phases. Intake and tagging are deliberately **not** interleaved —
downloading is network-bound, tagging is a mix of CPU-bound (Essentia,
fingerprinting) and rate-limited API calls. Separating them keeps
throttling manageable and gives clean resume points if a run dies partway.

```
┌──────────────┐     ┌──────────────┐     ┌────────────────┐
│    INTAKE     │ --> │   TAGGING    │ --> │  RESTRUCTURE    │
│  (download)   │     │  (per file)  │     │ (separate pass, │
│               │     │              │     │  after trust)   │
└──────────────┘     └──────────────┘     └────────────────┘
```

Within each phase, limited concurrency is fine (a few parallel downloads;
a few parallel API lookups) — tune once it's running on real hardware.

### Phase 1 — Intake

1. Accept one or many links — albums, playlists, individual tracks. Via
   CLI (flag or file of links) or web UI textarea.
2. Expand playlists/albums into individual entries (yt-dlp handles this).
3. **Dedup layer 1 — archive check.** Skip any video ID already in the
   local archive log (same pattern as yt-dlp's own `--download-archive`).
   Instant, no network call.
4. **Dedup layer 2 — metadata pre-check.** For anything new, fetch only
   title/uploader/duration (no audio download) and compare against the
   existing library. If it looks like something you already have,
   **pause and ask for confirmation** before skipping — never auto-skip
   here, since a shared title may be a legitimately different mix.
5. Download audio via yt-dlp. Embed source video ID for bookkeeping.

Dedup layer 3 (audio fingerprint) runs in Phase 2 as the final safety net
for anything layers 1 and 2 missed.

### Phase 2 — Tagging (per file)

1. **Video-rip detection.** Check filename and embedded title for signals
   ("Official Video", "Music Video", etc.). Matches are quarantined to
   `_review/possible-video-rip/` and skip the rest of the pipeline until
   you confirm. Prevents wasting API calls matching a music video's audio.

2. **Audio fingerprint** (Chromaprint / `fpcalc`). Local, no network,
   computed for **every** file. Serves two purposes:
   - **Dedup layer 3.** Compare against fingerprints already indexed. On a
     genuine duplicate, keep the higher-quality file: format tier first
     (lossless > lossy), then bitrate as tiebreak. Loser moves to a
     low-quality-duplicates folder or macOS Trash — never silently
     deleted outright.
   - **AcoustID lookup fallback.** Only queried over the network when
     title/artist search (step 4) comes up empty. Fingerprinting locally
     is cheap; it's the API lookup we defer.

3. **Local genre detection** (Essentia, `discogs-effnet` model). Local, no
   network. Three uses, and measured accuracy constrains each differently:
   - Selects which source-precedence list to query (detected "Deep House"
     → Discogs first). Safe: this needs only the **top-level** genre,
     which is ~87-91% right whenever confidence is above 0.25.
   - Acts as the **fallback genre tag** if no external source returns
     genre data. Use the top-level genre and only above a confidence
     floor — writing the full `Genre---Style` label would put
     `Pop---K-pop` on an American rapper, which the model does.
   - A cross-check: if Essentia says House and the matched release says
     Drum & Bass, that's a signal the *match* is wrong. Only meaningful
     when the detection was confident, or it will flag good matches.

   **Measured, not assumed** (against tracks with known genres): top-level
   genre is right ~22% of the time below 0.15 confidence, ~75% from
   0.15-0.25, ~87% from 0.25-0.40, and ~91% above 0.40. The style is
   noticeably weaker than the genre at every level. Both the label and its
   confidence are stored in `tracks`; anything acting on a detected genre
   must read both.

4. **Source matching.** Query sources in the detected genre's precedence
   order (see below). Pull every target field plus album art.

   Files in `beatport/` go through this **exact same process** — no
   special-casing. Their existing tags are treated as a strong search
   hint, the same as any other partially-tagged file.

5. **Confidence scoring.**
   - High confidence → auto-apply.
   - Low confidence or ambiguous → **review queue** (web UI).
   - No match → flag. You can mark a track "unofficial / self-made,
     won't match" so it stops re-flagging on future runs.
   - No ISRC found anywhere → leave blank, note it for manual follow-up.

6. **Backup.** Log existing tag values to SQLite *before* any overwrite.

7. **Write tags.** Overwrite with matched data, embed 640×640 album art.

### Phase 3 — Restructure (separate pass)

Run only once you trust the tags. Reorganizes into `Artist/Album/Track.ext`
**within** each source folder — `beatport/` and `yt-dlp/` stay separate and
are each cleaned internally.

Kept separate from tagging deliberately: moving files based on wrong
metadata is far more annoying to undo than fixing a wrong tag.

---

## Genre-based source precedence

One table, keyed by locally-detected genre. No folder-based override layer
is needed, since genre detection runs before matching regardless of which
folder a file came from.

```toml
# precedence.toml
[genres.electronic_house]
order = ["discogs", "musicbrainz", "spotify"]

[genres.rnb]
order = ["spotify", "musicbrainz", "itunes"]

[genres.default]
order = ["musicbrainz", "spotify", "itunes"]
```

Discogs leads for electronic genres: free and self-serve (no partner gate),
strong remixer/label/catalog-number data, and it shares a taxonomy with the
Essentia model, so local detection and source data speak the same
vocabulary.

This table is tuned using the **probe tool** — a permanent skill/command
that runs sample tracks against every source and prints a per-field
side-by-side, so precedence comes from real observed data rather than
guesswork.

## Source folder config

Separate concern from precedence — this is only "what to scan and how."

```toml
# sources.toml
[sources.beatport]
path = "~/Music/beatport"
check_for_video_rips = false

[sources.yt-dlp]
path = "~/Music/yt-dlp"
check_for_video_rips = true
```

New folders are added here by hand, or via a one-time interactive prompt
the first time the tool encounters an unregistered folder.

---

## State: SQLite

One database holds everything the tool needs to remember. Gitignored.

| Table | Holds |
|---|---|
| `tracks` | One row per known file: path, fingerprint, current tag snapshot, match status |
| `tag_history` | One row per **text-field** change: file id, timestamp, field, old value, new value |
| `download_archive` | Source video IDs already downloaded (dedup layer 1) |
| `wont_match` | Tracks you've marked unofficial/self-made, so they stop re-flagging |

`tag_history` being a table rather than JSON files is what makes full
per-file history practical: `music-match undo <file>` queries it, shows a
timeline, and lets you revert to any prior point rather than just the last
change.

### Album art storage — separate from tag_history, not a BLOB column

Art doesn't belong in `tag_history` directly — at ~50-150KB per 640×640
cover, multiplied across 2000 tracks and however many revisions each gets,
BLOB columns would bloat the database fast and slow down every query that
touches that table, even ones with nothing to do with art.

Instead: **content-addressed file storage.**

- Every embedded cover image is saved to
  `.music-match/art-store/<sha256-of-image-bytes>.jpg` (gitignored,
  alongside the DB).
- `tag_history` gets a row same as any other field change, but for
  `field = "album_art"`, `old_value`/`new_value` hold the **hash**, not
  the image bytes.
- This naturally **deduplicates**: every track on the same album shares
  one stored file instead of a dozen copies of the same cover. A track's
  history moving between three different candidate covers over multiple
  runs costs three small files total, not three files per run.
- Undo for art means: look up the hash from the history row at the
  target point, read that file, re-embed it. Same transaction as
  reverting the text fields, so a single `undo` call restores everything
  consistently — never text fields at one point in time and art at
  another.

---

## Interfaces

**CLI** — scripting and automation:
- Submit links (single or batch) for intake
- Run the probe/precedence tool
- Run a duplicate scan across the library
- Run the restructure pass
- `music-match reindex`
- `music-match undo <file>`
- Dry-run flag available on anything that writes

**Web UI** (NiceGUI) — interactive review:
- Review queue: low-confidence matches, no-match tracks, video-rip
  quarantine confirmations
- Edit any track's metadata directly, including album art
- Submit links to kick off intake
- Batch progress view (queued → downloading → tagging → done/flagged)

---

## Tech stack

| Piece | Choice | Why |
|---|---|---|
| Language/runtime | Python 3.14.x via `uv` | Confirmed `essentia`/`essentia-tensorflow` dev1438 ships cp314 macOS-arm64 wheels — see risk note below before assuming this is fully settled |
| Downloading | `yt-dlp` | Already your tool |
| Tag read/write | `mutagen` | Standard; handles ID3/MP4/WAV |
| Fingerprinting | Chromaprint (`fpcalc`) + AcoustID API | One library covers dedup and ISRC fallback |
| Local genre detection | Essentia, `discogs-effnet` model | Fine-grained taxonomy matching Discogs |
| Discogs | Free public API, self-serve key | Primary for electronic; matches Essentia taxonomy |
| MusicBrainz | `musicbrainzngs` or direct HTTP | No key needed; mind the rate limits |
| Spotify | Spotify Web API | Keys already available |
| iTunes | iTunes Search API | No auth needed |
| State | SQLite (`sqlite3` stdlib) | Full history, queryable |
| Web UI | NiceGUI | Built on FastAPI/uvicorn internally — one Python process, no separate frontend toolchain, no Node |
| CLI framework | `typer` | Type-hint-driven, fits the mypy setup |
| Config | TOML (`tomllib` stdlib for read) | Matches examples above |
| Formatting | `yapf`, Google style preset | 2-space indent, official Google tool |
| Linting | `pylint` + Google's published `.pylintrc` | Official Google config |
| Type checking | `mypy` | Data-model-heavy codebase |
| Testing | `pytest`, split fast/slow | See git workflow |

---

## Git & commit workflow

- **Branching:** feature branches + PRs, even solo. **One PR per
  build-order stage** (11 total) — not one per commit, not several
  stages bundled together.
- **Commit messages:** subject-line-only, short, lowercase, plain
  imperative ("add fingerprint-based dedup"). No body, no Conventional
  Commits prefixes. Enforced by a git-native `commit-msg` hook.
- **Authorship:** Claude never appears as author or co-author.
  `includeCoAuthoredBy: false` in `.claude/settings.json`.
- **No session traces in git history or PRs.** No "Generated with Claude
  Code" footers, no co-authored-by lines (already covered above), no
  links to the Claude Code session/transcript in commit messages or PR
  descriptions. A PR should read like you wrote it.
- **PR lifecycle — resolved.** Claude commits through a stage, opens the
  PR (description carries the real "what was accomplished" writeup —
  see CLAUDE.md's template), then does a **self-review pass** over the
  full accumulated diff before merging — this is the actual checkpoint,
  Claude's, not a human one. The review is recorded as a comment on the
  PR itself, not just noted in the description. (Not an approval:
  GitHub forbids approving your own PR, and Claude commits as the repo
  owner, so author and reviewer are one account.) Only after that does
  auto-merge run
  (`gh pr merge --auto --squash --delete-branch`), gated on CI passing.
  No human reviews before code is live — that's intentional. Full detail
  in CLAUDE.md's "PR lifecycle" section.
- **Quality gate on every commit:** yapf + pylint + mypy + fast unit tests
  must all pass. Enforced in two paired layers:
  - Git-native **pre-commit hook** (`pre-commit` framework) — catches
    commits from any source: Claude Code, terminal, or VS Code's git panel.
  - Claude Code `PreToolUse` hook scoped to `Bash(git commit *)` — second
    layer specific to Claude-initiated commits.
- **Test split:** fast unit tests (tag logic, config parsing, fingerprint
  comparison — no network) gate commits. Slow integration tests (real
  Discogs/Spotify/MusicBrainz calls) run in GitHub Actions on PRs only.
- **Remote:** private GitHub repo.

### .gitignore essentials

- Music files (any audio extension) — never in git
- `.env` and any real credentials
- The SQLite database
- Essentia model weights (large binaries, downloaded on setup)
- `_review/` quarantine folders and other runtime output
- Standard Python noise (`__pycache__/`, `.venv/`, `.mypy_cache/`,
  `.pytest_cache/`)

---

## Setup on a new machine

Gitignored state means a fresh clone starts with **no memory** of your
library. The bootstrap path:

1. Clone the repo.
2. Copy `.env.example` → `.env`, fill in real keys. `.env.example` is
   committed (variable names only, no values) so setup isn't guesswork.
3. `uv sync` — installs deps and triggers the Essentia model download.
4. Confirm the library is present. Source folders match by **name**, so
   this works wherever the library lives, as long as folder structure is
   intact.
5. `music-match reindex`:
   - Walks configured source folders.
   - Fingerprints every existing file and populates the dedup index. No
     re-downloading, no re-tagging.
   - Skips anything already fully tagged.
   - Rebuilds the download-archive log by reading back the embedded source
     video ID, so dedup layer 1 doesn't restart from zero.

`reindex` is also the recovery path if the database is ever lost or
corrupted — not just a new-machine step.

---

## Known risks and open questions

**WAV metadata support is genuinely weak.** WAV has no native tagging
standard; tags are bolted on via non-standard ID3 chunks, and support
varies by application. **Decision: convert the 8 Beatport WAVs to AIFF** —
lossless, same audio, but proper ID3 tag support. One-time job at that
volume, handled as a small utility rather than a pipeline stage.

**Rekordbox reload caveat.** Tracks you previously edited *inside*
Rekordbox may not pick up file-tag changes on "reload tags," since
Rekordbox can prioritize its own database. You've said removing and
re-importing is acceptable, so this is a known-and-accepted cost rather
than a blocker. The tool never touches the Rekordbox database directly —
that format is undocumented and corrupting it would be unrecoverable.

**Rate limits.** MusicBrainz in particular enforces strict limits. Any
run over ~2000 tracks needs backoff and resumability so a partial run
isn't wasted work.

**Essentia on Python 3.14 — resolved, and the risk note was wrong.**
Verified working: `essentia-tensorflow 2.1b6.dev1438` installs from a
prebuilt cp314 macOS-arm64 wheel, imports, and runs real inference on
Python 3.14.7 / macOS 26.6 arm64.

The "open upstream issue where `essentia.tensorflow` fails to import on
macOS ARM" recorded here was a misreading. **There is no
`essentia.tensorflow` module in any Essentia build** — the TensorFlow
algorithms are exposed through `essentia.standard`
(`TensorflowPredictEffnetDiscogs`, `TensorflowPredict2D`). That import
fails everywhere, working install or not, so it was never evidence of a
packaging bug. The real check is:

```bash
python -c "from essentia.standard import TensorflowPredictEffnetDiscogs"
```

The rest of the dependency tree resolved without trouble, so the first
`uv sync` did settle the 3.14 question as expected.

Essentia stays **out of `pyproject.toml`** deliberately: it is a 94MB
wheel needed by one optional command, and keeping it out means CI and a
default `uv sync` do not pay for it. Genre detection reports what to
install when it is absent.

The fallbacks that were held in reserve — an isolated older-Python
subprocess, or a different local classifier at the cost of losing the
Discogs taxonomy match — are not needed.

---

## Build order

1. **Skeleton.** Config loading (`sources.toml`, `precedence.toml`),
   SQLite schema, mutagen read/write wrapper, CLAUDE.md, and the full
   yapf/pylint/mypy/pytest toolchain wired into pre-commit + Claude hooks.
2. **Fingerprinting + dedup scan.** Standalone-testable, no API deps.
3. **Local genre detection** (Essentia). Also standalone-testable. Prove
   the install works early, since it's the riskiest dependency.
4. **Probe tool.** Build as `.claude/skills/probe/SKILL.md`, not just a
   CLI subcommand — it's something you'll return to repeatedly as source
   data quality shifts, not a one-time setup step. Lets you start tuning
   `precedence.toml` on real data before the matcher is built.
5. **Source matching + confidence scoring.** The largest piece.
6. **Tag writing** + album art + backup/undo logging.
7. **Video-rip detection.**
8. **Intake pipeline.** Multi-link, archive check, metadata pre-check,
   download.
9. **Web UI.** Review queue first, then link submission.
10. **Restructure pass.** Last — only sensible once tagging is trusted.
11. **`reindex`** + GitHub Actions integration-test workflow. Can run in
    parallel with the above; blocks nothing.

Steps 2–4 are deliberately front-loaded: each is independently useful,
independently testable, and free of external API dependencies, so you get
working tools early and de-risk Essentia before committing to it.
