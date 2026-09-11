# todo

`[ ]` open · `[x]` done · `[>]` awaiting human verification · **cp** = checkpoint, a hard stop (see `plan.md`)

## phase 0 — walking skeleton

- [x] **t1** scaffold `src/music/`, uv project, one passing test
  - accept: `uv run pytest` green; `ruff format --check` green; mypy green
  - verify: write a 2-space-indented file, confirm ruff keeps it at 2
- [x] **t2** `db/`: schema.sql (§11), migrate, connection module
  - accept: all 7 tables created; migrate is idempotent
  - verify: run twice, second is a no-op; `sqlite3 .schema` matches §11
- [x] **t3** stage runner: resumable, driven by `track.stage`
  - accept: kill mid-run, restart, resumes at the same track
  - verify: integration test kills after track 3 of 5, asserts resume at 4
- [x] **t4** `acquire`: one youtube url → staging file + `source_file` row
  - accept: file on disk; row has sha256, duration, itag, channel
  - verify: `-m live` test on one art track
- [x] **t5** transcode → aiff (`pcm_s16be`) + write `TIT2`/`TPE1`
  - accept: output is 16-bit 44.1 stereo; both frames read back
  - verify: unit test on ffmpeg-synthesized audio (§19) — no committed binaries
- [x] **t6** publish to `library/` (flat for now; layout comes in t23)
- [x] **t7** wire `music ingest <url>` through all of the above
- [x] **cp1** import the result into rekordbox **and** serato; both display it

## phase 1 — normalise + accuracy spike

- [x] **t8** `normalise.py` + golden-file tests
  - accept: seeded with real failures — `LMFAOVEVO`,
    `Calvin Harris - I Need Your Love (Official Video)`, `Burnie, Phoenix Ho`
  - verify: golden table passes; 90% coverage (§19); pure, no i/o
- [x] **t9** musicbrainz adapter behind the §7 protocol, pydantic models
  - accept: returns `FieldCandidate`s; malformed json raises at the boundary
  - verify: cassette test, no network
- [x] **t10** cache + rate limit + backoff
  - accept: second identical lookup makes zero http calls
  - verify: unit test counts calls; backoff test simulates 503
- [x] **t11** confidence scoring (§12), pure
  - accept: table from §12 reproduced exactly; version-variant guard penalises
    `(instrumental)` when unrequested
  - verify: unit tests per row; the `SMASH!` case is a regression test
- [x] **cp2** **informational** — measured **61.5%** (96 tracks, musicbrainz
  only, pre-redownload audio). see `tasks/cp2-results.md`. the binding
  go/no-go moved to **cp4**, where all five sources and clean audio exist.

## phase 2 — acquisition is real

- [x] **t12** format chain `141/774/140/251`, `web_music` client, browser cookies
- [x] **t13** quality gate: hard-fail <256 kbps
- [x] **t14** `music doctor`: ffmpeg, yt-dlp, cookies, models, disk space
- [x] **t15** playlist enumeration
- [x] **t16** pre-download dedup on `video_id`
- [x] **t17** art-track preference + music-video detection (§8 signal table)
- [x] **cp3** **PASS** — 13 files all itag 141, re-run downloaded nothing,
  the known music-video url was flagged. `tools/gate_ingest.py`.

## phase 3 — resolution is real

- [x] **t18** acoustid fingerprinting → stable id (needs api key)
- [x] **t19** discogs adapter + cassettes
- [x] **t20** spotify adapter + cassettes
- [x] **t21** itunes search adapter + cassettes (artwork)
- [x] **t22** url override: spotify/mb/discogs/beatport link → exact identity
- [x] **cp4** **BINDING GO/NO-GO** — **PASSED at 77.1%** (seed 7, binding) and
  80% (seed 99). re-measure the same 100 tracks with all
  sources + art-track audio. ≥75% proceed · 65–75% revise §9 · <65% rethink.
  ci green with no network.

## phase 4 — classify + arbitrate

- [x] **t23** `classify.py`: essentia → top-level genre → family (§7)
  - accept: `Style` is never written as a tag
- [x] **cp5** **bollywood gate** — failed at 37%, fixed with an ISRC
  country override, now **87% PASS**. see SPEC.md §7.
- [x] **t24** `arbitrate.py` + `precedence` table, pure
  - accept: confidence gates entry, not ranking (§12); first source wins
  - accept: album fields resolved as a group from one release; deluxe
    preferred; dates still come from the release-group's first release (§7)

## phase 5 — publish quality

- [x] **t25** all 18 id3v2.4 frames (§10) incl. both `TDRC` and `TDRL`
  - verify: round-trip test asserts every frame survives
- [x] **t26** canonical naming (`ft.`, `[remix]`) + genre/artist layout (§14)
- [x] **t27** `retag` and `resolve --redo`
  - accept: never overwrites `decided_by='manual'`
- [>] **cp6** verified in rekordbox + serato: fields populate correctly.
  **accuracy defects found and fixed** — compilations winning over albums,
  `Not On Label`, curly quotes, deluxe editions ignored across sources.
  outstanding: re-verify in both apps, and the retag-preserves-manual-edit step.

## phase 6 — web ui

- [x] **t28** review queue + audio preview
- [x] **t29** metadata editor; edits are sticky and re-tag the file
- [x] **t30** provenance panel: what each source said, per field
- [x] **t31** elicitation mode → populates `precedence`
  blind comparison (no source named in page or payload), stratified per
  (field × family) cell, shrunk toward the built-in ranking, thin cells left
  alone. `music serve` → Calibrate.
- [x] **t31b** add tracks from the web ui — paste a youtube link, worker
  thread, live progress. ingest extracted from `cli` into `pipeline`.
- [ ] **cp7** review 50 real items, measure seconds/item vs §9's assumed 35 s

## phase 7 — the real run

- [ ] **t32** elicitation session (~120 tracks, ~90 min)
- [ ] **t33** full run over the playlists
- [ ] **t34** `music add` — 7 beatport wavs, 1 soundcloud track
- [ ] **cp8** library complete; queue drained; total manual time vs 5-hour budget
