# music-match

Automatically tags a personal music library (~2000 tracks) with accurate
metadata from public music databases, so files work cleanly in Rekordbox.
Also handles fresh downloads end-to-end: paste links → download → tag.

See @ARCHITECTURE.md for the full design, pipeline phases, and build order.

## Tech stack

- Python 3.14.x, managed by `uv` (not pip, not poetry)
- `typer` for CLI, `NiceGUI` for the web UI (built on FastAPI/uvicorn
  internally — one Python process, no separate Node toolchain)
- `mutagen` for tag read/write
- `yt-dlp` for downloading
- Chromaprint (`fpcalc`) + AcoustID for fingerprinting
- Essentia (`discogs-effnet`) for local genre detection
- SQLite (stdlib `sqlite3`) for all persistent state
- TOML for config (`tomllib` for reads)

## Commands

- Install deps: `uv sync`
- Run CLI: `uv run music-match <command>`
- Format: `uv run yapf -i -r src/ tests/`
- Lint: `uv run pylint src/ tests/` (scope must match CI and the pre-commit hook exactly — never lint a narrower or wider set in one layer than another)
- Type check: `uv run mypy src/`
- Fast unit tests: `uv run pytest tests/unit/`
- Integration tests (network): `uv run pytest tests/integration/`
- All commit checks: `uv run yapf -d -r src/ tests/ && uv run pylint src/ tests/ && uv run mypy src/ && uv run pytest tests/unit/`

## Project structure

- `src/music_match/cli/` — typer commands
- `src/music_match/web/` — NiceGUI app and review UI
- `src/music_match/intake/` — link parsing, dedup layers 1-2, yt-dlp download
- `src/music_match/tagging/` — fingerprinting, genre detection, matching, tag writing
- `src/music_match/sources/` — one module per metadata source (discogs, musicbrainz, spotify, itunes)
- `src/music_match/db/` — SQLite schema and queries
- `src/music_match/config/` — TOML loading for sources.toml and precedence.toml
- `tests/unit/` — fast, no network, gates commits
- `tests/integration/` — real API calls, CI only

## Code style

- **2-space indentation.** Google Python style guide, enforced by `yapf`
  (google preset).
- Google-style docstrings with Args/Returns/Raises sections.
- Type hints on all function signatures. `mypy` must pass.
- Explicit imports. No wildcard imports.

## Conventions

- All persistent state lives in SQLite, never scattered JSON files.
- Config is TOML. Never hardcode paths — read them from `sources.toml`.
- Source folders are matched by **name** (`yt-dlp`, `beatport`), not by
  absolute path, so the library can move between machines or drives.
- Every tag write logs prior values to `tag_history` **before** overwriting.
- Anything that writes to disk needs a `--dry-run` flag.
- Network calls need backoff and must be resumable. A partial run over
  2000 tracks should never be wasted work.
- MusicBrainz enforces strict rate limits. Respect them.

## Git workflow

- Feature branches, then PRs. Never commit directly to `main`.
- Commit messages are **subject line only** — short (under 60 chars),
  lowercase, plain imperative ("add fingerprint-based dedup", not "Add
  Fingerprint-Based Dedup" or "Added fingerprint dedup"). No commit body,
  no Conventional Commits prefixes like `feat:` or `fix:`. This is
  enforced by a git-native `commit-msg` hook
  (`.claude/hooks/commit-msg-check.sh`), which rejects non-conforming
  commits regardless of how they were made.
- **Never** add Claude as an author or co-author on commits.
- **Never** reference the Claude Code session in a commit message or PR
  description — no "Generated with Claude Code" footers, no session or
  transcript links. Write it like you wrote it.

### PR lifecycle — one PR per build-order stage

Each of the 11 items in ARCHITECTURE.md's build order gets exactly one
PR, not one PR per commit and not several stages bundled into one PR.

1. **Work the stage.** Commit as you go — multiple commits on one
   feature branch is expected and normal. Each commit still passes the
   full quality gate (yapf, pylint `src/ tests/`, mypy, `tests/unit/`)
   individually.
2. **Open the PR** once the stage is complete. PR description follows
   the template below — this is where the real explanation lives, not
   in any individual commit.
3. **Self-review before merging — do not skip this.** Read the full
   accumulated diff for the stage (`gh pr diff`), not just each commit
   in isolation — bugs that span multiple commits, or that only become
   visible once you look at the whole stage together, are exactly what
   this step exists to catch. If you find something, fix it with an
   additional commit on the same branch and re-review.
4. **Record the review formally**, once you're satisfied:
   ```bash
   gh pr review --approve --body "reviewed: <what you checked, what you
   found and fixed if anything, why this stage is ready>"
   ```
   This is a real GitHub approval, visible on the PR — not just a note
   in the description. It's what "approve the PR" means here.
5. **Merge:**
   ```bash
   gh pr merge --auto --squash --delete-branch
   ```
   GitHub merges once the required CI check passes. There is no *human*
   review checkpoint — the self-review in steps 3-4 is the checkpoint,
   and it's Claude's, not the user's. That's intentional, not an
   oversight. If CI fails, the PR stays open; fix and push again, which
   reruns checks and auto-merge retries.

### PR description template

```markdown
## Summary
<What this stage accomplishes, in plain language — this is the
"here's what was accomplished" writeup. Commits stay terse; this is
where the real explanation lives.>

## Changes
- <roughly one bullet per commit, or per logical change>

## Notes
<Anything a reviewer would want to know: tradeoffs made, things
deliberately left out of scope for this stage, follow-ups.>
```

The self-review verdict itself goes in the `gh pr review --approve`
body (step 4 above), not in this description — keeps "what changed"
and "what was checked before merging" as two distinct, separately
visible things on the PR.

## mutagen typing pattern

`mutagen` ships `py.typed`, but `FileType.tags` is typed
`Optional[Tags]` without narrowing per-subclass — `isinstance(audio,
MP3)` does not narrow `tags` to non-`None`, so relying on isinstance
alone produces mypy `warn_unreachable` false positives on the format
branches. Always go through a helper that asserts explicitly instead:

```python
def get_tags(audio: mutagen.FileType) -> mutagen.Tags:
  """Returns audio.tags, creating an empty tag container if absent.

  Args:
    audio: A loaded mutagen file object.

  Returns:
    The file's tag container, guaranteed non-None.
  """
  if audio.tags is None:
    audio.add_tags()
  assert audio.tags is not None
  return audio.tags
```

`pyproject.toml` disables `warn_unreachable` specifically for
`music_match.tagging.*` as a safety net — but that's a backstop, not a
substitute for using this pattern everywhere tags are touched.

## README.md — keep it current

`README.md` currently exists but is empty. It needs to become the
human-facing front door to this repo — what a visitor (including future
you) sees first on GitHub. It is **not** a duplicate of `ARCHITECTURE.md`
or this file; keep the split clean:

- **README.md** — what the project is, how to install it, the CLI
  commands that exist today, and a link to `ARCHITECTURE.md` for anyone
  who wants the full design.
- **ARCHITECTURE.md** — the deep design doc. Not meant for a first-time
  visitor.
- **CLAUDE.md** — instructions for Claude, not really meant for humans
  at all.

**Requirement: update `README.md` in the same PR as any change that
affects what a user of this repo sees or does** — a new CLI command, a
new config option, a changed setup step, a new environment variable.
Treat a PR that adds user-facing behavior without a README update as
incomplete, the same way you'd treat it as incomplete without tests.

This is enforced by judgment, not a lint rule — "did the README need to
change" isn't mechanically checkable the way formatting is. If genuinely
unsure whether a change is README-worthy, err toward updating it.

## Skills

Repeatable procedures live in `.claude/skills/<name>/SKILL.md`, not
copy-pasted into CLAUDE.md. Two are expected early, matching the build
order:

- **`probe`** (build order step 4) — runs sample tracks against every
  configured metadata source and prints a per-field side-by-side, so
  `precedence.toml` gets tuned from real data. This is referenced
  throughout this doc as "the probe tool"; it should be built as an
  actual skill, not just a CLI subcommand buried in help text, since
  you'll return to it repeatedly as source data quality shifts over time.
- **`add-metadata-source`** — the checklist for adding a new source
  module under `src/music_match/sources/`: implement the common source
  interface, register it in `precedence.toml`, add unit tests with
  fixture responses (never hit the real API in a unit test), update
  README if it's user-facing. Worth writing as a skill once the first
  two or three sources (Discogs, MusicBrainz, Spotify) establish the
  actual pattern to codify — premature before that.

## MCP servers

**None configured. Decision, not an oversight:**

- **SQLite** — no MCP server for this. Claude Code already has Bash
  access and can query the `.db` file directly with the `sqlite3` CLI
  (`sqlite3 music_match.db "SELECT ..."`) — an MCP server only earns its
  keep when the model has no other way to reach the data, which isn't
  the case here. Also worth knowing: Anthropic's own reference SQLite
  MCP server was moved to an archived, unmaintained repo with no
  security guarantees, and the third-party alternatives found on a
  search were inconsistent and hard to vet. Skip it.
- **Playwright** — legitimate, actively maintained by Microsoft
  (`@playwright/mcp`), but nothing to test until the web UI (build order
  step 9) exists. Revisit there, not before — wiring it up against an
  empty project just means it sits unused. When you do get there, note
  that even Microsoft's own docs point out MCP loads large tool schemas
  and accessibility trees into context on every turn; a CLI+skill
  approach can be more token-efficient for a coding agent specifically —
  worth weighing both at that point rather than defaulting to MCP.

## Things to avoid

- Never touch files outside the folders configured in `sources.toml`.
- Never touch the Rekordbox database directly. The format is undocumented
  and corruption is unrecoverable. This tool writes file tags only.
- Never delete an audio file outright. Duplicates move to a
  low-quality-duplicates folder or the macOS Trash.
- Never commit audio files, `.env`, the SQLite DB, or Essentia model weights.
- Don't add dependencies without asking first.
- Don't scrape Beatport. Discogs is the primary source for electronic genres.
