# music-match

Automatically tags a personal electronic-music library with accurate
metadata pulled from Discogs, MusicBrainz, Spotify, and iTunes, so files
work cleanly in Rekordbox. Also handles fresh downloads end-to-end: paste
a link, it downloads via `yt-dlp`, matches it against public sources, and
writes the tags.

Full design: [ARCHITECTURE.md](./ARCHITECTURE.md)

## Status

Early development. In place: config loading, the SQLite schema, the tag
read/write layer, audio fingerprinting with a duplicate scan, and local
genre detection. Not yet: metadata matching, tag writing from sources,
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
  listed.

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
