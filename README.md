# music-match

Automatically tags a personal electronic-music library with accurate
metadata pulled from Discogs, MusicBrainz, Spotify, and iTunes, so files
work cleanly in Rekordbox. Also handles fresh downloads end-to-end: paste
a link, it downloads via `yt-dlp`, matches it against public sources, and
writes the tags.

Full design: [ARCHITECTURE.md](./ARCHITECTURE.md)

## Status

Early development. The skeleton is in place — config loading, the SQLite
schema, and the tag read/write layer — but no matching, downloading, or
fingerprinting yet. The commands below are the ones that actually exist.

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
  reconfiguration. Nothing outside these folders is ever touched.
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
```

Useful flags:

- `config show --sources PATH --precedence PATH` — point at config files
  other than the ones in the working directory.
- `db init --db PATH` — use a database somewhere other than
  `./music_match.db`. `db init` is safe to re-run; it never touches
  existing rows.
- `db init --dry-run` — report what would be created without writing.
  Every command that writes to disk has a `--dry-run`.
- `tags show --show-empty` — include fields the file leaves unset.

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
built at test time, so nothing here needs ffmpeg or a network connection.

## License

Personal project, not currently licensed for reuse.
