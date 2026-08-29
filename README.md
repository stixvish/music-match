# music-match

Automatically tags a personal electronic-music library with accurate
metadata pulled from Discogs, MusicBrainz, Spotify, and iTunes, so files
work cleanly in Rekordbox. Also handles fresh downloads end-to-end: paste
a link, it downloads via `yt-dlp`, matches it against public sources, and
writes the tags.

Full design: [ARCHITECTURE.md](./ARCHITECTURE.md)

## Status

Early development. In place: config loading, the SQLite schema, the tag
read/write layer, and audio fingerprinting with a duplicate scan. Not yet:
genre detection, metadata matching, tag writing from sources, downloading,
or the web UI. The commands below are the ones that actually exist.

## Setup

See [SETUP.md](./SETUP.md) for first-time setup on a new machine. The
short version:

```bash
brew install uv chromaprint jq
uv sync
cp .env.example .env   # then fill in real API keys
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
built at test time and `fpcalc` is stubbed out, so nothing here needs
ffmpeg, Chromaprint, or a network connection.

## License

Personal project, not currently licensed for reuse.
