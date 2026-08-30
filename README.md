# music-match

Automatically tags a personal electronic-music library with accurate
metadata pulled from Discogs, MusicBrainz, Spotify, and iTunes, so files
work cleanly in Rekordbox. Also handles fresh downloads end-to-end: paste
a link, it downloads via `yt-dlp`, matches it against public sources, and
writes the tags.

Full design: [ARCHITECTURE.md](./ARCHITECTURE.md)

## Status

Early development. In place: config loading, the SQLite schema, the tag
read/write layer, audio fingerprinting with a duplicate scan, local genre
detection, and the four metadata sources with a probe tool for comparing
them. Not yet: matching and confidence scoring, tag writing from sources,
downloading, or the web UI. The commands below are the ones that actually
exist.

## Setup

See [SETUP.md](./SETUP.md) for first-time setup on a new machine. The
short version:

```bash
brew install uv chromaprint jq
uv sync
cp .env.example .env   # then fill in real API keys
```

Genre detection additionally needs Essentia and its models. Both are
optional — everything else works without them — and Essentia is
deliberately not in `pyproject.toml`, since it is a 94MB wheel used by one
command:

```bash
uv pip install essentia-tensorflow
uv run music-match genre fetch-models
```

## Configuration

Two TOML files, both committed, both edited by hand:

- **`sources.toml`** — which folders to scan and how to treat them. Source
  folders are matched by **name** (`yt-dlp`, `beatport`), not by absolute
  path, so the library can move between machines or drives without
  reconfiguration. Nothing outside these folders is ever touched. A
  `[duplicates]` table sets where the lower-quality copy of a duplicate is
  moved to; keep it outside the source folders, or the next scan will
  index those files again.
- **`precedence.toml`** — which metadata sources to query, in what order,
  for a given locally-detected genre. `order` is the per-genre default;
  a `[genres.<name>.fields]` table overrides it for individual fields
  where one source is reliably better (Discogs for `remixer`, say).
  `[genres.default]` is required — it is the fallback for any genre not
  listed. It is tuned from real probe output rather than assumptions, and
  every entry carries a comment recording what was measured.

Credentials live in `.env`, never in the TOML files. See
[`.env.example`](./.env.example) for the variables you need.

## Commands

```bash
uv run music-match version              # installed version
uv run music-match config show          # resolved config, both files
uv run music-match db init              # create the SQLite database
uv run music-match tags show FILE       # a file's tags, as the tool sees them
uv run music-match scan                 # fingerprint the library
uv run music-match dedup                # report duplicate recordings
uv run music-match genre show FILE      # what the model hears in one file
uv run music-match genre index          # detect genres across the library
uv run music-match genre summary        # what the library is made of
uv run music-match probe FILE...        # compare all four metadata sources
uv run music-match match show FILE      # what one file would be matched to
uv run music-match match run            # match the library, record proposals
uv run music-match match summary        # how many matched / need review
```

### Finding duplicates

`scan` fingerprints every audio file in the configured source folders with
Chromaprint and records the result. It is resumable — files already
fingerprinted are skipped — so an interrupted run over a few thousand
tracks is never wasted work. Roughly 0.1s per file.

`dedup` then compares those fingerprints and reports recordings held more
than once, including copies whose filenames and tags give no hint they are
the same. It **reports only** by default:

```bash
uv run music-match dedup            # show what it found, touch nothing
uv run music-match dedup --apply    # move the lower-quality copies out
```

The copy kept is the higher-quality one: lossless beats lossy whatever the
bitrates say, then bitrate breaks the tie. The loser is **moved** to the
`[duplicates]` folder, never deleted.

### Genre detection

`genre` runs Essentia's `discogs-effnet` model locally — no network, no API
key. It predicts from a 400-label vocabulary in Discogs' own
`Genre---Style` form:

```
$ uv run music-match genre show "Lil Uzi Vert - Paradise.m4a"
  0.233  Hip Hop---Cloud Rap
  0.146  Pop---K-pop
  0.078  Electronic---Synth-pop

precedence key: hip_hop
```

Choosing this model is what lets local detection and Discogs speak the
same vocabulary. The **precedence key** is the top-level genre reduced to
the form `precedence.toml` is keyed on, so a detected
`Electronic---Deep House` selects `[genres.electronic]` without the config
having to list every style. The fifteen keys the model can produce are:

```
blues              classical    folk_world_country  jazz    non_music  reggae
brass_military     electronic   funk_soul           latin   pop        rock
childrens          hip_hop      stage_screen
```

`genre index` walks the library and records a label per track, resumable
the same way `scan` is. Roughly 1.3s per file.

Essentia's own logging is silenced while the model runs: its TensorFlow
algorithms emit `No network created, or last created network has been
deleted...` *per analysis frame*, which buries the progress output under
thousands of lines over a library-sized run. Set
`MUSIC_MATCH_ESSENTIA_LOGS=1` to get it back when debugging. Two
TensorFlow startup lines (`absl::InitializeLog`, `mlir_graph_optimization`)
are emitted from C++ before any Python control exists and cannot be
suppressed without also hiding real errors, so they stay.

**How accurate is it?** Measured against tracks with known genres, the
**top-level genre is right about three quarters of the time overall — but
that number hides everything useful.** Accuracy tracks confidence closely:

| Confidence | Top-level genre correct |
|---|---|
| below 0.15 | ~22% — the model is guessing |
| 0.15 – 0.25 | ~75% |
| 0.25 – 0.40 | ~87% |
| above 0.40 | ~91% |

So the label is only worth as much as the score beside it, which is why
both are stored. `genre summary` reports the mean confidence per label and
takes `--min-confidence`.

**The style is markedly less reliable than the genre.** `Rock---Hard Rock`
for AC/DC and `Hip Hop---Gangsta` for 2Pac are right, but AC/DC's "T.N.T."
comes back `Rock---Pub Rock`, and melodic pop-rap is drawn to
`Pop---K-pop` regardless of who made it. Treat the top-level genre as
usable and the style as a hint — which is all this stage needs it to be,
since precedence is keyed on the top level alone.

### Metadata sources

Four public databases are wired up, each behind the same interface:

| Source | Auth | Reliably carries | Never carries |
|---|---|---|---|
| **discogs** | token | genre/style, `remixer`, `composer`, `mix_name`, label, catalogue no. | `isrc`, `release_date` |
| **spotify** | client id + secret | `isrc`, `release_date`, track/disc numbers, 640px art | credits, genre |
| **itunes** | none | `album`, `release_date`, track/disc numbers, coarse genre, art | `isrc`, credits |
| **musicbrainz** | none (user agent) | artist credits, `track_total` | art; `isrc` only sometimes |

Requests are rate limited per source (MusicBrainz allows one a second and
enforces it), retried with backoff on 429 and 5xx, and cached for seven
days under `.music-match/http-cache/` so re-running a probe is cheap.

### Probing sources

`probe` asks every source about the same tracks and prints a per-field
comparison plus a coverage table. This is how `precedence.toml` gets
tuned — from observed data rather than from guesses about which database
ought to be better:

```bash
uv run music-match probe ~/Music/yt-dlp/*.m4a       # queries built from tags
uv run music-match probe --artist X --title Y       # ad-hoc, no file needed
```

```
  isrc
    discogs      -
    musicbrainz  -
    spotify      GBDUW0600009
    itunes       -

=== coverage across 10 track(s) ===
  field             discogs    musicbrainz    spotify       itunes
  isrc                0/10         4/10        10/10         0/10
  remixer             2/10         0/10         0/10         0/10
```

There is a `probe` skill in `.claude/skills/probe/` covering how to choose
a sample and how to read the output — including the trap that coverage is
not quality.

### Matching

`match` decides which release each track actually is, and how much to
trust the answer. It records a **proposal** — nothing is written to any
audio file, so a doubtful match can sit in the review queue harmlessly.

```bash
uv run music-match match show FILE   # one file, with the scoring shown
uv run music-match match run         # the whole library, resumable
uv run music-match match summary     # matched / review / no_match counts
uv run music-match match ignore FILE --reason "self-made edit"
```

Each source is asked for several candidates rather than one, because
taking each platform's top hit is only about half right — they rank for
popularity, store relevance and text score, none of which is "the release
this file came from". Candidates are then scored on:

- **Duration**, the strongest signal. A live cut runs minutes longer than
  the studio take; nothing in the text can tell you that.
- **Title and artist similarity**, after normalising away case, accents,
  featured artists and yt-dlp filename noise.
- **Penalties** for release shapes that are usually the wrong answer —
  soundtracks, greatest-hits, karaoke, live albums, remix EPs — unless the
  track being matched is itself one of those.

Where the sources **disagree**, the majority wins over configured
precedence. This is the case scoring cannot solve: searching AC/DC's
"Thunderstruck" returns both `The Razors Edge` and the `Iron Man 2`
soundtrack with identical title, artist *and* duration. Nothing separates
them except that other sources agree on one of them.

**Confidence** blends how well candidates scored, how much the sources
agreed, and how many answered. At or above `--auto-apply` (0.85) the match
is trusted; below it goes to review; below `--review-floor` (0.45) it is
discarded. Measured against tracks with known answers, every correct match
scored 0.90 or better and a genuinely ambiguous one — a radio edit that
four sources placed on four different releases — scored 0.81. That gap set
the threshold, but the sample was small; expect to revisit it.

A file carrying **no title tag at all** is searched for by its file name
rather than skipped — nearly everything here is named `Artist - Title`,
and for the WAV files that name is the only metadata they have.

**What confidence does not measure:** whether the *release* is the one you
would have picked. It measures whether this is the right recording and
whether the sources agree. A track can be confidently matched to a
compilation that legitimately contains it.

Useful flags:

- `config show --sources PATH --precedence PATH` — point at config files
  other than the ones in the working directory.
- `db init --db PATH` — use a database somewhere other than
  `./music_match.db`. `db init` is safe to re-run; it never touches
  existing rows.
- `db init --dry-run` — report what would be created without writing.
  Every command that writes to disk has a `--dry-run`.
- `tags show --show-empty` — include fields the file leaves unset.
- `scan --source NAME` — scan one configured folder rather than all.
- `scan --limit N` — stop after N files, for a quick look.
- `scan --force` — re-fingerprint files already indexed.
- `scan --dry-run` — report what would be fingerprinted without writing.
- `dedup --threshold F` — how similar two tracks must be to count as the
  same recording (default 0.85; real duplicates measure 0.95-1.0 and
  unrelated tracks 0.0).
- `genre show --top N` — how many predictions to print.
- `genre index --source NAME`, `--limit N`, `--force`, `--dry-run` — as
  for `scan`.
- `genre fetch-models --models DIR` — put the models somewhere other than
  `./models`.
- `probe --only discogs,spotify` — ask a subset of sources.
- `probe --all-fields` — include fields no source answered.
- `probe --no-cache` — bypass cached responses, for checking whether a
  source's data has actually changed.
- `match run --source NAME`, `--limit N`, `--force`, `--dry-run` — as for
  `scan`.
- `match run --auto-apply F --review-floor F` — move the confidence
  thresholds.
- `match show --genre "Electronic"` — pick the precedence order by genre
  without needing the track indexed.

## Development

```bash
uv run yapf -i -r src/ tests/   # format (Google style, 2-space indent)
uv run pylint src/ tests/       # lint
uv run mypy src/                # type check
uv run pytest tests/unit/       # fast tests, no network
uv run pytest tests/integration/  # real API calls, CI only
```

All four of the first checks gate every commit, via both a `pre-commit`
hook and CI. Unit tests are hermetic: the audio fixtures they use are
built at test time, and `fpcalc` and the genre model are stubbed out, so
nothing here needs ffmpeg, Chromaprint, Essentia, or a network
connection.

## License

Personal project, not currently licensed for reuse.
