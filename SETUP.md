# Setup

**Audience: you, primarily.** Two steps below need a human specifically
— `gh auth login` (interactive browser login) and typing real values
into `.env` (your credentials). Everything else is just bash; once
those two are done, you can either keep running the rest yourself or
open Claude Code and ask it to run through the remainder — it has full
Bash access and nothing else here requires you specifically.

## First time on a new machine

1. **Clone the repo.**

2. **System dependencies** (macOS, via Homebrew):

   ```bash
   brew install uv chromaprint jq
   ```

   - `uv` — Python/dependency management
   - `chromaprint` — provides `fpcalc` for audio fingerprinting
   - `jq` — used by the Claude Code hook scripts to parse JSON

3. **Python dependencies:**

   ```bash
   uv sync
   ```

4. **Pull Google's official pylint config, then patch the one real
   conflict with our yapf setup:**

   ```bash
   curl -fsSL https://raw.githubusercontent.com/google/styleguide/gh-pages/pylintrc -o .pylintrc
   ```

   Google's file ships `indent-string='    '` (4 spaces), which
   contradicts `.style.yapf`'s 2-space indent — yapf formats to 2, then
   pylint flags the formatted code as wrong. Patch it:

   ```bash
   sed -i '' "s/indent-string='    '/indent-string='  '  # LOCAL CHANGE: 2-space, matches .style.yapf/" .pylintrc
   ```

   **If you ever re-fetch `.pylintrc` from upstream, re-run this patch.**
   The `curl` overwrites the file wholesale and will silently reintroduce
   the conflict.

5. **Install the git hooks.** Two separate installs — `pre-commit`
   only wires up the default stage by itself; `commit-msg` needs its own
   flag or the message-format hook silently never runs:

   ```bash
   uv run pre-commit install
   uv run pre-commit install --hook-type commit-msg
   ```

6. **Secrets:**

   ```bash
   cp .env.example .env
   ```

   Fill in real values. `.env` is gitignored and must never be committed.

   Which key unlocks what: `DISCOGS_TOKEN` enables the Discogs source,
   `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` enable Spotify, and
   `ACOUSTID_API_KEY` is for the fingerprint lookup fallback. iTunes and
   MusicBrainz need no key. A source without its credentials reports
   itself unavailable rather than failing mid-run, so a partial `.env`
   still works.

   **`MUSICBRAINZ_USER_AGENT` should name you, not the placeholder.**
   MusicBrainz requires a user agent identifying the client and a real
   contact, and throttles generic ones. `.env.example` ships
   `music-match/0.1.0 ( your-email@example.com )` — replace the address
   with one you actually read.

7. **Confirm your library is present.** Source folders are matched by
   name (`yt-dlp`, `beatport`), so the library can live anywhere as long
   as the folder structure is intact. Adjust paths in `sources.toml` if
   needed.

8. **Rebuild local state:**

   ```bash
   uv run music-match reindex
   ```

   Fingerprints existing files, populates the dedup index, and rebuilds
   the download-archive log. No re-downloading, no re-tagging. This is
   also the recovery path if the database is ever lost or corrupted.

## One-time GitHub setup: branch protection on `main`

Claude is configured to open every PR with
`gh pr merge --auto --squash --delete-branch`. On its own, GitHub's
auto-merge does **not** wait for anything, and nothing stops a plain
`git push origin main` from bypassing the PR flow entirely — branch
protection is what makes both of those actually true, not just assumed.

Do this once, after your first push to GitHub (the CI workflow needs to
have run at least once for GitHub to know its job name):

```bash
gh api repos/stixvish/music-match/branches/main/protection \
  --method PUT \
  --input - << 'EOF'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["checks"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
```

What each piece actually does:

- `required_status_checks` — merges (auto or manual) wait for the
  `checks` job to pass. `"checks"` must match the job id in
  `.github/workflows/ci.yml`; if you rename that job, update this too
  and re-run the command.
- `required_pull_request_reviews` with `required_approving_review_count:
  0` — this is the one that actually blocks direct pushes to `main`.
  Every change must go through a PR to land, but zero approvals are
  required, which is what makes this consistent with the earlier
  decision: auto-merge with no manual review checkpoint, while still
  closing the "someone/something just pushes straight to main" gap.
- `enforce_admins: true` — the protection applies to you too, not just
  Claude. Set `false` if you want an escape hatch for yourself.
- `allow_force_pushes: false`, `allow_deletions: false` — `main` can't be
  rewritten or deleted, by anyone.

Also confirm auto-merge is allowed on the repo itself (usually on by
default for personal repos, but worth checking):

```bash
gh repo edit stixvish/music-match --enable-auto-merge
```

**Worth sitting with before you rely on this:** this setup means code
lands on `main` with no human review checkpoint — the CI gate (lint,
types, unit tests) is the only thing standing between a chunk of Claude's
work and being live. That's a deliberate choice for a solo project, but
it's a meaningfully different posture than "Claude opens a PR and I look
at it" — there's no longer a point where you're looking at it before it
ships.

## Essentia, for genre detection

Genre detection (`music-match genre`) needs Essentia and about 20MB of
model files. Both are **optional** — every other command works without
them — so Essentia is deliberately not in `pyproject.toml`. That keeps
`uv sync` and CI free of a 94MB wheel.

```bash
uv pip install essentia-tensorflow
uv run music-match genre fetch-models
```

Verify the install with:

```bash
uv run python -c "from essentia.standard import TensorflowPredictEffnetDiscogs"
```

**Not `import essentia.tensorflow`.** Earlier notes in this repo said to
check that, citing an upstream macOS ARM packaging bug. That check was
wrong: there is no `essentia.tensorflow` module in *any* Essentia build,
so it fails on every platform whether or not anything is broken. The
TensorFlow algorithms live in `essentia.standard`. Confirmed working
here on Python 3.14.7, macOS 26.6 arm64, `essentia-tensorflow
2.1b6.dev1438` — installed cleanly, imported, and ran real inference.

`uv pip install` puts Essentia in the venv without recording it in
`pyproject.toml`, so a later `uv sync` will remove it. Re-run the install
if `genre` starts reporting Essentia missing.

`fetch-models` downloads the discogs-effnet embedding model and the
genre_discogs400 classifier head into `models/`, which is gitignored. It
skips files already present, so it is safe to re-run.
