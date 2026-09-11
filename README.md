# music-match

turns a youtube playlist into a correctly tagged aiff library that rekordbox 7
and serato dj lite both read in full.

the hard part is not downloading. it is that no single catalogue knows every
track, they contradict each other constantly, and a confidently wrong answer is
worse than no answer — it lands in the library looking finished. most of this
repository is about deciding which source to believe, and about noticing when
none of them should be.

```
                                            ┌──────────────┐
  youtube ──► acquire ──► normalise ──► identify ──► arbitrate ──► publish
                 │                        │            │             │
              staging/                 6 sources    precedence     library/
              (m4a)                                 + consensus     (aiff)
                                           │
                                      not confident?
                                           │
                                      review queue ──► web ui
```

everything is driven from one sqlite database. **the database is the source of
truth and the audio files are a regenerable projection of it.** that is not a
slogan: a resolver improvement is applied to an existing library with
`music retag --rearbitrate`, offline, in seconds, with no re-download and no
api calls.

---

## the pipeline, stage by stage

### 1. acquire — `acquire/youtube.py`

yt-dlp fetches itag 141 (aac 256 kbps, 44.1 khz) through the `web_music` player
client, using premium cookies read from chrome. two things must both be present
or the premium formats silently do not appear at all: `deno` on `PATH` plus the
`yt-dlp-ejs` package, which together solve youtube's javascript "n" challenge.
the symptom of either being missing is `Requested format is not available` on a
video the cli downloads fine.

the download is checked against a bitrate floor and **fails loudly** rather than
quietly accepting a downgrade. `acquire/classify_source.py` flags music-video
rips, which carry applause, intros and dialogue that make a mess of both
fingerprinting and duration matching.

audio lands in `~/Music/.staging` as `.m4a` and stays there. it is the input to
everything downstream and to the web ui's audio preview.

### 2. normalise — `normalise.py`

a youtube channel is not an artist and a youtube title is not a title. this
stage turns `Pitbull - Fireball ft. John Ryan (Official Video) [HD]` and the
channel `PitbullVEVO` into `artist=Pitbull, title=Fireball`.

it strips vevo/topic suffixes, `Artist - ` prefixes, production noise and
`feat.` clauses — but **keeps mix designations**, because `[Tom Santa Remix]` is
part of which recording this is, not decoration.

this stage is worth +31 percentage points of match rate, measured. without it
the whole review budget collapses.

### 3. identify — `identify.py`, `resolve.py`, `sources/`

six sources, each behind an adapter with its own rate limiter and a shared
sqlite response cache (`sources/cache.py`), so re-running costs nothing.

| source | gives | notes |
|---|---|---|
| **acoustid** | recording identity from the audio itself | chromaprint fingerprint; the only source that knows what the file *contains* |
| **musicbrainz** | credits, release data, isrc | authoritative on featured vs collaborating artists |
| **spotify** | album, isrc, artwork | flattens features into the artist list |
| **itunes** | album, artwork, regional catalogue | strongest artwork; best coverage of non-western releases |
| **discogs** | style, label, catalogue number | the only good source for label on electronic material |
| **beatport** | genre, label, mix name, bpm, key | adapter built; inactive pending partner credentials |

**every source is asked for several results and every result is scored.** they
used to return five and keep the first, which is frequently a remaster, a live
cut or a re-recording with the original further down — `Love Story (Taylor's
Version)` ahead of `Love Story`. one function, `identify.match_score`, answers
"which of these is the right one" everywhere:

```
0.7 * (0.7 * exact_title + 0.3 * core_title) + 0.3 * artist
  − 0.25  a version designation the query never asked for
  − 0.12  an unrequested variant word (live, acoustic, instrumental…)
```

title dominates and artist only supports, because the query artist is often a
channel name — `jayseanworldwide`, `push baby`, `iyazlive`.

the fingerprint path is **gated, not just ranked**: an acoustid entry links many
recordings in no meaningful order, so all of them are scored and the match is
*refused* if none resembles what we searched for, falling back to text search.

### 4. confidence — `identify.py`

every identity gets a score, and the score decides whether a human sees it.

| evidence | confidence |
|---|---|
| pasted url override | 1.00 |
| isrc | 0.98 |
| clean fingerprint | 0.95 |
| corroborated by two agreeing catalogues | 0.90 |
| strong text search | 0.85 |
| anything doubtful | 0.50 |

auto-accept is 0.80. two signals **veto** everything above them:

- **`variant_mismatch`** — the match adds a version qualifier we did not ask
  for. being confidently wrong about which cut this is is worse than asking.
- **`artist_unrelated`** — the match carries the right title and a stranger's
  name. this is the cover signature, and title scoring cannot see it.

corroboration is the interesting one. a doubtful fingerprint is not an unknown
identity: if two catalogues independently agree on artist and title *and* that
agreement matches the query, the identity is settled. but corroboration
**cannot lift either veto**, because a fingerprint is *acoustic* evidence about
this file while catalogue agreement is *bibliographic* evidence about the song.
two catalogues confirming that taylor swift recorded a track does not make this
file her recording of it. an earlier version missed that distinction and
silently returned three covers to auto-accept.

### 5. arbitrate — `arbitrate.py`

each field is decided independently, from a per-(genre family × field)
precedence table. genre selects the table; the table ranks the sources.

three rules stop individually defensible answers from being collectively wrong:

**agreement outweighs precedence.** two sources naming the same value beat one
ranked higher. this is the single most valuable rule in the file — without it,
musicbrainz ranks first for pop and published `Last Night` by morgan wallen as
`California` by metro station, over itunes and spotify which both had it right.
matching is tried in three tiers: exact, then with version qualifiers stripped,
then on the core alone (`JAY-Z & Kanye West` and `JAY-Z` share a core).

`genre` and `artwork_url` are exempt — sources differ in granularity there by
design, and two coarse sources saying "Dance" must not outrank discogs'
"Progressive House".

**album fields resolve as a group, from one source.** taking the album name from
a deluxe edition and the track number from the standard yields track 14 of a
12-track album. editions are grouped even without brackets, so `Nothing But the
Beat`, `Nothing but the Beat 2.0` and `Nothing But the Beat Ultimate` are one
release and the fullest edition wins — while `Kidz Bop 22` and `Kidz Bop 21`
stay different records.

**compilations are refused.** a track's album is the album it belongs to, not a
licensing compilation it appears on. when every source offers only a
compilation, the album fields are dropped: an empty album is visibly incomplete
and gets fixed in review, a wrong one propagates into the filename, the folder
and both dj apps.

dates bypass precedence entirely — §7 wants the *earliest* release of the
recording, so `_earliest` picks it, preferring precision when the year ties.

**manual edits are never overwritten.** re-running resolution must always be
safe; that property is what makes database-as-source-of-truth worth having.

### 6. publish — `publish/`

ffmpeg decodes to **16-bit aiff**. aiff rather than flac because rekordbox
silently drops album artist, release date, original artist, mix name and
lyricist from flac, two of which are required fields — measured, not assumed.
`-sample_fmt s16` is mandatory: ffmpeg defaults to 24-bit when decoding aac,
inflating output ~40% for no benefit from a lossy source.

tags are id3v2.4. `publish/tag.py` **preserves frames it does not own** —
serato stores beatgrids and cue points in `GEOB` frames in the same tag, and an
ordinary delete-then-write destroys them.

files are named and filed in house style:

```
library/<genre-family>/<artist>/Artist - Title (ft. X) [Y Remix].aiff
```

features use `ft.` in parentheses, remixes go in `[brackets]`, and credited
artists render as `A`, `A & B`, `A, B & C`.

### 7. review — `web/`

a local fastapi app, no auth, no network exposure. it is a correction surface,
not a player or a library browser.

- **queue** — everything the pipeline was not sure about, with the reason
- **audio preview** — non-negotiable; the common failure is a plausible match
  that is the wrong recording, and you have to hear it
- **provenance** — what every source said for every field, and what arbitration
  would pick, so a wrong tag is one click from *why*
- **editing** — sticky, marked `manual`, never overwritten by a later run
- **artwork** — candidate covers shown as images, not urls
- **search** — by resolved artist, title, album or label, and also by the name
  the track was searched under, which is how a bad match gets chased down
- **ingest** — paste youtube links, one per line, with the pipeline's real
  output streamed into a console and per-stage counters
- **calibrate** — elicitation (below)

### 8. calibrate — `elicit.py`

the built-in precedence rankings are reasoned guesses. calibration measures the
real preference by showing the values two or more sources offered **with no
source named anywhere** — not in the page, not in the payload.

blindness is the whole method. a labelled comparison measures which source you
trust; an unlabelled one measures which value is actually better. answers are
not revealed per question either, because "you picked discogs" would anchor the
next fifty answers toward consistency rather than judgement.

sampling is stratified per (field × family) **cell**, not per track — a random
sample of 120 tracks would ask `genre` for pop eighty times and never once for
world. each cell retires at eight answers, which is also what makes the session
finite.

derivation shrinks toward the built-in ranking, so a cell with no evidence
reproduces the default exactly and a source that keeps winning climbs past
sources ranked above it. below five answers a cell is **not written at all** —
a ranking derived from two answers is worse than a reasoned default.

---

## commands

```bash
uv run music doctor                     # check ffmpeg, fpcalc, credentials, disk
uv run music ingest <url> [--limit N]   # download a playlist and run the pipeline
uv run music serve                      # the review and calibration ui
uv run music status                     # counts by stage and status
uv run music retag [--rearbitrate]      # re-emit tags; optionally re-decide first
uv run music reset [--keep-staging]     # delete everything and start over
```

`retag --rearbitrate` is the one worth knowing. `field_candidate` is
append-only and already holds every value every source offered, so improving the
resolver and re-running it corrects an existing library without touching the
network.

## layout

```
src/music/
  acquire/     yt-dlp, source classification, audio probing
  normalise.py youtube title and channel -> searchable artist and title
  sources/     one adapter per catalogue, each rate-limited and cached
  identify.py  scoring: match_score, confidence, the two vetoes
  resolve.py   orchestration: fingerprint, then text, then enrichment
  arbitrate.py which source wins each field
  elicit.py    blind calibration of the precedence table
  publish/     transcode, tag, name, file
  web/         review ui
  db/          schema and migrations; the only module that owns sqlite
  pipeline.py  the run loop, driven by both the cli and the web ui
```

`SPEC.md` is what we are building and the evidence for every claim in it.
`CLAUDE.md` is how we work. `tasks/` holds the plan and the task list.

## setup

needs python 3.14, [uv](https://docs.astral.sh/uv/), ffmpeg, fpcalc
(chromaprint), deno, and a chrome signed into youtube premium.

```bash
uv sync
uv sync --group classify      # optional: essentia genre routing (~20 mb of graphs)
uv run music doctor
```

credentials go in `.env` (gitignored) or `~/.config/musicpipeline/config.toml`.
none are required to start: missing credentials disable their source rather
than failing the run.

```
ACOUSTID_API_KEY=       # free, and by far the highest-value one
DISCOGS_TOKEN=          # free
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
```

**no secrets in the repository, ever** — no cookie files, no tokens. gitleaks
runs on every commit.

## accuracy, measured

the first hundred-track run published **83 tracks, 14 of them the wrong song**,
every one at confidence 0.95. all fourteen came through the fingerprint route,
were described faithfully by musicbrainz, and won on precedence over two
sources that had it right.

| | first run | now |
|---|---|---|
| published as the wrong song | 14 | 0 |
| covers reaching the library | 6 | 0 |
| auto-accept | 95% (false) | 86.8% |
| review queue | 20 | 14 |

auto-accept *fell*, and that is the honest outcome: the 95% was never real. the
remaining 14 review items are all genuine — six covers, four duration
mismatches, three version mismatches, and one renamed band (`push baby` is
rixton) which is a known false positive the rule accepts in order to never let a
cover through.

## development

```bash
uv run pytest -q            # 481 tests
uv run ruff check .
uv run mypy src/music
uv run python tools/check_spec.py
```

lefthook runs lint, format, gitleaks, the spec cross-reference check, mypy and
the full test suite on every commit.

two working rules this project has learned the hard way:

- **measure, do not assume.** two confident decisions have already been
  reversed by measurement — flac as the target format, and opus having a
  lowpass. if a claim can be tested locally in under an hour, test it before
  writing it into the spec.
- **every claim in `SPEC.md` cites its evidence.** if it cannot be cited, it is
  marked as projected.
