# music metadata pipeline — spec

**Status:** format probe COMPLETE · AIFF confirmed · no open probe items
**Last updated:** 2026-09-11

## 1. problem

~2,300 tracks sourced from YouTube via yt-dlp, in M4A/AAC. The container cannot
hold the metadata fields required for DJ use, and identity/credits are
incomplete. Rekordbox 7 and Serato DJ Lite must both display a complete,
correct tag set.

## 2. goals

- Every track carries a complete tag set in **Rekordbox**, and as complete a set
  as **Serato DJ Lite** can display (it has no columns for album artist, mix
  name, original artist, lyricist, disc, or ISRC — see §10).
- Point at a YouTube playlist and get a tagged library out.
- Point at a local file (or a Spotify/MusicBrainz/Discogs URL) and get the same.
- Metadata is regenerable: improving the resolver re-tags the library for free.
- Manual review stays within a **5-hour total budget** (§9).

## 3. non-goals (v1)

- Tidal, Bandcamp, Apple Music API — gated or paid. Deferred to v2.
- Cue points, beatgrids, crates — owned by the DJ apps, not this tool.
- **Playlist provenance** — playlists will be rebuilt by hand in Rekordbox.
- **`tutorial-tracks/`** — excluded from the pipeline entirely.
- **Hosting / multi-tenancy** — local-first by decision, not by omission (§13).
- **Migrating the existing 2,329 M4A files** — they are being re-downloaded.
  Their tags are deliberately discarded (§4, Option C).
- 100% composer/lyricist coverage — best-effort, never blocking.

## 4. verified findings (evidence base)

| Finding | Evidence |
|---|---|
| Library is 2,329 files, 100% M4A/AAC, 16 GB | full ffprobe scan |
| 98.5% already at 255–256 kbps (YouTube's max, itag 141) | audio-stream bitrate scan |
| 75.4% carry ISRC; 72.2% complete on core 8 fields | tag coverage scan |
| M4A cannot hold remixer/label/original artist/mix name | Pioneer metadata spec |
| 256 kbps reachable via `web_music` + Premium cookies | downloaded & verified 256012 bps |
| **Opus 774 retains MORE ultrasonic content than AAC 141** — the steep roll-off is AAC's 44.1 kHz Nyquist wall, not an Opus lowpass | spectral measurement, same track, both streams |
| Neither difference is audible (all of it above 20 kHz); both are transparent at 256 kbps | — |
| **Opus cannot encode 44.1 kHz at all** — the codec supports 48 kHz only, so YouTube resamples every 44.1 kHz master up to 48 kHz for itag 774. The AAC stream (141) stays native 44.1 kHz. | xiph/opus issue #43; mastering-forum corroboration |
| itag 141 chosen for **sample-rate fidelity** (44.1 kHz matches the master; the Opus 48 kHz is manufactured) and because AIFF at 48 kHz is 8.8% larger | measurement + arithmetic |
| 117 files are music-video rips (channel name as artist, inflated duration) | filename/tag pattern scan |
| Library is 85% non-electronic (hip-hop/pop/R&B/Bollywood) | genre distribution |
| essentia-tensorflow works on Python 3.14.7; emits Discogs taxonomy | installed, ran on library files |
| FLAC 16-bit = 57.7 GB vs AIFF 79.4 GB | measured on probe files |
| **Rekordbox reads ALL required fields from AIFF/ID3v2.4** | format probe, Rekordbox 7 |
| **Rekordbox FLAC drops album artist, release date, original artist, mix name, lyricist** | format probe; 3–4 uppercase key spellings tried per field |
| Lyricist DOES round-trip via ID3 `TEXT` (contra forum reports) | format probe |
| Comment key differs by app in FLAC: Rekordbox `COMMENT`, Serato `DESCRIPTION` | format probe |
| **A fresh yt-dlp download yields NO ISRC, album artist, track, disc, composer, lyricist or key; genre is literally `"Music"`** | downloaded an art track with `--embed-metadata` |
| **Query hygiene is worth +31pp of resolution accuracy** (24% → 55% high-confidence) | 30-track MusicBrainz sample, before/after cleaning |
| Un-cleaned failures are VEVO channel names and `Artist - ` title prefixes | same sample |
| Residual ambiguity is duration mismatch on music-video rips, not identity doubt | same sample (`rivals=0, dur_ok=False`) |
| MusicBrainz misses remixes/edits (`(LEFTI REMIX)`, `- H.K.G Mix`) | same sample |
| Text match can confidently return the wrong *version* (`SMASH!` → `SMASH! (instrumental)`) | same sample |

## 5. architecture

Source of truth is **SQLite**. Audio files are a regenerable *projection* of it.
Re-tagging never requires re-downloading or re-resolving.

```
music ingest <playlist-url>     music add <file|dir>     music add --url <link>
        │                              │                          │
        ├─ enumerate                   │                          │
        ├─ dedup check ← PRE-download  │                          │
        ├─ download (141/774/140/251)  │                          │
        ├─ quality gate (<256k = fail) │                          │
        └───────────────┬──────────────┴──────────────────────────┘
                        ▼
      probe → normalise → classify → resolve → arbitrate
                        ▼
           transcode → AIFF · write ID3v2.4 · publish
```

Three entry points, one pipeline. `music add` (local file) is what converts the
7 purchased Beatport WAVs and the SoundCloud track; it is the ingest path minus
the download stage. `music add --url` supplies an authoritative identity
directly and skips resolution.

Every stage is **resumable**: each track's stage marker is a DB row, so a crash
at track 1,800 of 2,329 resumes at 1,800.

## 6. pipeline stages

1. **Enumerate** — playlist → video IDs + title/channel/duration.
2. **Dedup** — skip anything already owned (§8). Runs pre-download.
3. **Download** — yt-dlp Python API (not subprocess: structured info dict).
   Format chain `141/774/140/251`; `web_music` client; cookies read from
   the browser at runtime (§13) — no credential file exists.
4. **Quality gate** — hard-fail below 256 kbps rather than silently degrade.
5. **Normalise** — clean artist/title before any query. Strip `VEVO` suffixes,
   a redundant leading `Artist - `, `(Official Video)` / `(Lyric Video)` /
   `(Visualizer)`, and trailing `feat.` clauses; take the primary artist only.
   **Measured worth +31pp of resolution accuracy (§4) — the highest-leverage
   stage in the pipeline.**
6. **Classify** — Essentia `genre_discogs400`. **Only the top-level genre is
   used** (the part before `---`), to route precedence. The `Style` half is
   unreliable and is never written as a tag (§7). This produces a
   **provisional** family.
7. **Resolve identity** — AcoustID fingerprint → normalised text search →
   manual URL override. Emits a confidence score (§12). Penalise version
   variants (`(instrumental)`, `(sped up)`) the query did not ask for.
8. **Arbitrate** — per-(genre-family → field) precedence (§12). The family is
   **finalised here, not at step 6**: the ISRC country override (§7) needs an
   ISRC, which only exists once identity is resolved. A fresh download carries
   none.
9. **Transcode** — ffmpeg → **AIFF** (`-c:a pcm_s16be`), native sample rate
   (no resampling). 16-bit is correct: the source is lossy AAC.
10. **Tag** — ID3v2.4 frames (§10).
11. **Publish** — canonical naming + layout (§14), record provenance. Tracks below
    the confidence threshold are **not published**; they go to the review
    queue (§9) and stay out of the library until resolved.

## 7. sources and precedence

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

**Genre selects the table; the table ranks sources per field.**

```
family = classify(audio)                 # local, source-independent, pre-API
for field in FIELDS:
    ranked = PRECEDENCE[family][field]   # e.g. ["discogs","musicbrainz","spotify"]
    value  = first source in `ranked` holding a candidate
```

Genre family comes from the *local* classifier, not from a source. This breaks
the circular dependency where you would need genre to pick the precedence
table, but genre is itself one of the contested fields.

**The classifier is a router, not a genre source.** Its `Style` output (the
half after `---`) is not trustworthy — it splits near-identical tracks across
`Trap` / `Cloud Rap` / `Pop Rap` on uncalibrated activations. **`Style` is never
written as a tag.**

Its **top-level genre** is used for two things: routing precedence, and — only
when no real source supplied a genre at all — as a last-resort tag value. A
coarse-but-plausible `Hip Hop` beats the blank field or the literal `"Music"`
that a fresh download provides. It never outranks a real source.

**Six genre families**, taken from the Discogs top-level genre (the part before
`---`) and collapsed:

`electronic` · `hip-hop` · `pop` · `r&b-soul` · `world` · `other`

**The classifier cannot route regional music, and ISRC country fixes it.**

cp5 measured Bollywood scattering across three families — `pop` 40%, `world`
37%, `electronic` 23% — with predicted styles of *K-pop*, *Laïkó*, *Pachanga*
and *Reggaeton*. The Discogs-400 model has **no Indian class**, so it reaches
for the nearest neighbour it knows. That is not noise; it is confident and
wrong.

It matters because `world` is the table that ranks **iTunes first**, which is
where Indian music is actually catalogued. Misrouting to `pop` sends those
tracks to a Spotify-first table instead.

The fix is an **ISRC registrant-country override**: an `IN`/`LK`/`PK`/`BD`
prefix routes to `world` regardless of what the classifier said. This does not
reintroduce the circular dependency the classifier exists to break — ISRC comes
from identity resolution, not from a contested metadata field. It does mean the
family is **finalised during arbitration, not during classification** (§6).

```
cp5 before override:  world 37%   scattered, FAIL
cp5 after override:   world 87%   PASS
```

ISRC must come from **resolution**, never from the file. A fresh yt-dlp
download carries no ISRC (§4), and the existing tags are being discarded, so a
gate that reads the file measures a condition that will never occur. Measured
on a fresh-download simulation, resolution recovers ISRC for **19 of 20**
Bollywood tracks — AcoustID carries it where text search is weak — and **17 of
20** carry an `IN` prefix.

> **Limitation:** ISRC registrant country is not music origin. `Saree Ke Fall
> Sa` resolves to `GBSGZ1300125` — a UK-registered release of Indian music — so
> the override does not fire. Those tracks fall back to the classifier, which is
> the correct failure mode but not a correct answer.

| Field | Leading sources (to be calibrated, §9) |
|---|---|
| artist / title / mix name | Beatport (electronic) → Discogs → MusicBrainz → Spotify |
| featured vs. collaborating artists | **MusicBrainz** (models artist-credit; Spotify flattens) |
| genre / style | Beatport *(electronic)* → Discogs Style → MusicBrainz → *(last resort)* Essentia top-level |
| label / catalog no. | Discogs → Beatport |
| release date | MusicBrainz release-group (original, not reissue) → Spotify |
| artwork | iTunes Search API |
| ISRC | existing tag → Spotify → MusicBrainz |
| BPM | local (Essentia). **Rekordbox overwrites `TBPM` with its own analysis; only Serato honours the tag** (§10) |
| key | local (Essentia). Honoured by *both* apps, and Serato DJ Lite cannot detect key at all — so `TKEY` is load-bearing |

### compilations must not supply album fields

Measured in cp6, on real output:

```
Club Can't Handle Me  ->  "NRJ Hits 2011"                     (Various Artists)
Fireball              ->  "Mastermix Classic Cuts, Vol. 165"  (Music Factory)
```

Both won on precedence while Spotify and iTunes held the actual albums. A
compilation is **a place the track appears, not the album it belongs to**.

Two signals, the second far more general:

1. an explicit various-artists credit
2. **an album artist that is not the track artist.** DJ-service compilations
   like Mastermix never say "Various Artists" — they are credited to the
   service. If the track is by Pitbull, the album it belongs to is credited to
   Pitbull. A wider credit still matches: `David Guetta` against
   `David Guetta & Akon` is the same album.

A compilation is used only when no other source offers album fields at all.

**Agreement between sources outweighs precedence.** Two sources naming the same
album beat one ranked higher naming a different one:

```
Hey Baby   musicbrainz: "Global Warming"                  <- was chosen, and wrong
           itunes + spotify: "Planet Pit (Deluxe Version)" <- correct
```

**The deluxe preference applies across sources, not only within MusicBrainz.**
Editions of one album group together — `Planet Pit` and `Planet Pit (Deluxe
Version)` are one release — and the largest edition wins inside the group. This
was specified in §7 from the start but implemented only inside the MusicBrainz
adapter, so `One Love (Deluxe)` lost to `One Love` even with two sources
offering it.

**`Not On Label` is not a label.** Discogs writes it for self-released and
white-label pressings; it is accurate and useless, and is dropped along with
`Self-Released` and `None`.

**Typographic quotes are normalised to ASCII.** MusicBrainz returns
`Club Can’t Handle Me`, which never matches a DJ-software search for
`Can't`.

### release selection

Album fields are **resolved as a group, from one chosen release** — never
arbitrated field by field. Taking the album name from a deluxe edition and the
track number from the standard produces metadata that is individually defensible
and collectively wrong.

**Prefer the deluxe edition.** Downloads land on whichever release a source
happens to return first, so without a rule a single album fragments: some tracks
filed under *Planet Pit*, others under *Planet Pit (Deluxe)*, and the album view
splits in two. Choosing the largest edition every time keeps it whole.

Selection, among releases containing this recording:

1. Restrict to release-groups of primary type **Album**. Compilations, live
   albums and greatest-hits collections are excluded unless the recording
   appears nowhere else — otherwise a 40-track *Greatest Hits* wins on size.
2. Prefer the release with the **most tracks**. This is more robust than
   keyword matching, which misses non-English and inconsistent edition naming.
3. Tie-break on edition keywords: `deluxe`, `expanded`, `special`,
   `anniversary`, `complete`, `extended`.

From the chosen release, atomically: `album`, `album_artist`, `track_number`,
`disc_number`. If the deluxe edition is chosen, the **deluxe edition's** track
and disc numbers are used — they are the numbering that matches the album the
track is filed under.

**Dates do not follow the chosen release.** `year` and `release_date` are the
**earliest release date of the recording, across every release it appears on** —
singles and EPs included, not just albums.

This matters because most singles precede their album:

```
"Give Me Everything"
  single       2011-03-18   <- release_date / year
  Planet Pit   2011-06-17
  Planet Pit (Deluxe)  2011-06-17   <- album, track_number, disc_number
```

Taking the album's date would date the song three months late, and taking a
deluxe reissue's date could be a year or more out. The album fields describe
*where the track is filed*; the date describes *when the music came out*. They
are answers to different questions and must be sourced separately.

Guard against bad catalogue data: ignore dates before 1900 or in the future, and
prefer a date with full day precision over a bare year when both exist.

### why beatport matters, measured

Two reasons, and the first was initially misread.

**Sub-genre taxonomy.** Genre labels embedded in *purchased files* are coarse —
`Dance / Pop`, `Dance / Electro Pop` — which suggested Beatport was blunter than
Discogs Style. That is only true of the file tags. The v4 API exposes a
**separate sub-genre resource** (`/v4/catalog/genres/{id}/sub-genres`), so the
fine taxonomy exists; purchased-file tags simply carry the top-level genre.

**DJ-specific versions.** Beatport catalogues extended mixes, club mixes and
DJ edits as first-class releases, for tracks that have no such version anywhere
else. These are precisely the cuts a DJ plays and precisely what `mix_name` and
`remixer` are for (§14) — so Beatport is not just a better genre source, it is
often the *only* source that knows the version in hand exists.

**Coverage of recent digital-only electronic releases**, where Discogs is a
physical-media-first database and has nothing:

```
7 purchased beatport tracks (2023-2026):   discogs found style for 1  (14%)
25 general electronic tracks (mixed era):  discogs found style for 22 (88%)
```

Future downloads are mostly electronic, so this gap widens over time rather
than closing. Beatport therefore ranks **first for genre on electronic**, and
Discogs remains first elsewhere.

### beatport access and adapter shape

Three documentation sites, all readable with an ordinary Beatport account:

| Site | Contents |
|---|---|
| `api.beatport.com/v4/docs/` | the catalog API reference |
| `account.beatport.com/docs/` | Identity Service OpenAPI — the auth endpoints |
| `partnerportal.beatport.com` | integration guides, scopes, token lifetimes |

**Auth lives on a different host from the catalog.** Tokens come from
`account.beatport.com/o/token/`; API calls go to `api.beatport.com/v4/`.

Grant flows: authorization code (PKCE **mandatory**), user password, and
**client credentials** — the last is the right one here, since this is a
server-side tool with no user context and no redirect URI.

Operational facts that shape the implementation:

- **Access tokens last 600 seconds.** A full-library run takes hours, so the
  adapter must refresh proactively rather than on 401.
- **Refresh tokens are single-use.** Each refresh returns a new one and revokes
  the old; the new token must be persisted immediately or access is lost until
  a fresh authorization. They last 31 days.
- Scopes (`app:externaltrusted`, `user:dj`, `openid`) determine which routes a
  token can reach, so available fields depend on what is granted.

What is *not* self-serve is the OAuth application itself.
`/v4/auth/o/applications/` returns `404`, an unauthenticated catalog call
returns `{"detail":"Authentication credentials were not provided."}`, and every
doc page refers to `{client_id provided}` and a `redirect_uri` "shared with
us". The credentials come from Beatport.

The public store site is separately behind a bot challenge, so scraping it is
not an alternative route.

**The adapter keys on ISRC, not text search.** `/v4/catalog/tracks/store/{isrc}`
resolves a track by ISRC directly — an exact join, not a fuzzy match. Resolution
already recovers ISRC for ~97% of tracks (§4), which makes this the single
highest-precision lookup available from any source. Text search
(`/v4/catalog/search`) is the fallback for the rest.

Endpoints the adapter needs:

| Purpose | Endpoint |
|---|---|
| Primary lookup | `/v4/catalog/tracks/store/{isrc}` |
| Fallback lookup | `/v4/catalog/search` |
| Track detail | `/v4/catalog/tracks/{id}` |
| Sub-genre taxonomy | `/v4/catalog/genres/{id}/sub-genres` |
| Token | `/v4/auth/o/token/` (client credentials) |

The transport is the only unimplemented piece; everything above is designed
against the documented contract. This is the reason §7 isolated Beatport behind
an adapter in the first place.

### artist query strategy

**Query the primary artist, not the full credit.** Measured on 40 multi-artist
tracks: primary found the recording and full credit found nothing **29 times**;
full credit never won outright and never scored higher.

The reason is separator convention. Tags carry Apple/YouTube's form —
`Alesso; Tove Lo`, `Drake; Yebba` — and MusicBrainz does not index that string
at all, so `artist:"Alesso; Tove Lo"` returns nothing while `artist:"Alesso"`
returns 100.

`&` is the exception, because MusicBrainz uses it too. On 50 `&`-joined tracks:
6 ties, 3 primary-only, 1 full-only. So the full credit is retained and retried
**only when the primary-artist query returns nothing** — cheap, occasionally
useful, never the first choice.

Collaborators are then recovered from the catalogue rather than the filename,
which is also where the featured-vs-collaborating distinction comes from.

**Rate limiting and caching** (§13) are part of this layer, not an afterthought:
every source adapter backs off exponentially and every response is cached to
disk, because resolution will be re-run many times as precedence is tuned.

## 8. identity and deduplication

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

On a duplicate hit: prefer higher bitrate; if equal (the common case once
everything is itag 141), prefer the **art-track** source over a video rip; if
still tied, keep the incumbent. Always log the decision.

**Music-video detection** (duration alone is insufficient):

| Signal | Meaning |
|---|---|
| channel is `X - Topic` | art track — clean audio (strongest positive) |
| `Provided to YouTube by...` in description | label-delivered audio |
| title matches `Official (Music )?Video\|M/V\|Visualizer\|Lyric Video` | video rip |
| `artist` tag equals channel name | video rip |
| duration vs. catalog duration > ~5 s | intro/outro/skit |

Prefer art tracks at *search* time; detection is the safety net.

## 9. review budget — 5 hours total

| Activity | Budget | Rate | Volume |
|---|---|---|---|
| Precedence elicitation (one-time) | 90 min | ~45 s | ~120 tracks |
| Review queue | 210 min | ~35 s | ~360 items |

Requires **≥84% auto-accept**. Under Option C there are no ISRCs to lean on, so
this is projected from the measured no-ISRC path:

```
MusicBrainz text, cleaned queries            55% high-confidence   (measured, n=29)
  + clean art-track audio (fixes the
    duration-mismatch AMBIG bucket)        → ~75-80%               (projected)
  + AcoustID, Spotify, iTunes, Discogs     → ~85%                  (projected)
                                           → review ≈ 350 ≈ 3.4 h  ✓
```

**This only works with the normalisation stage (§6.5).** At 24% — the
un-cleaned rate — review would be ~8 hours and the budget would be blown.

**Manual URL override is a budget lever**, not just a convenience: pasting a
Spotify/Discogs link resolves a track exactly, turning the hardest review items
(remixes MusicBrainz cannot find) from ~90-second puzzles into ~10 seconds.

**The review queue triggers on identity uncertainty, not field disagreement.**
If the recording is known, field conflicts resolve silently via precedence.
Genre disagreements never enter the queue — genre is subjective and would
consume the entire budget.

Expected outcome: **98–99% correct**, ~25–35 tracks with one wrong field.
Reaching 99.9% would need 20+ hours. The cheaper lever is re-running resolution
later against the DB.

**Elicitation method:** for each disputed field, present the candidate values
side by side **with no source named at all** — not in the page, not in the
payload. A labelled comparison measures which source the user trusts; an
unlabelled one measures which value is actually better, and only the second is
worth writing down. Answers are not revealed per question either: "you picked
Discogs" would anchor the next fifty answers toward consistency rather than
judgement.

**Stratification is per (field × family) cell, not per track.** A random sample
of 120 tracks would ask `genre` for pop eighty times and never once for world.
Each cell is asked until it has `TARGET_PER_CELL` (8) answers, then retires,
which is also what makes the session finite.

**Only fields that precedence decides, and that a human can judge, are asked.**
`release_date` and `year` are excluded because `_earliest` decides them and the
ranking is never consulted; `bpm` and `key` because they come from local
analysis; `album_artist`, `track_number` and `disc_number` because they move
atomically with `album` and are shown as part of that one question. `isrc` and
`catalog_number` are excluded on the second ground: an identifier is right or
wrong rather than better or worse, and nobody can tell which by reading it —
asking would collect coin flips and record them as preference.

**Derivation shrinks toward the built-in ranking.** A cell's score per source is

```
(wins + PRIOR_WEIGHT * prior) / (appearances + PRIOR_WEIGHT)
```

where `prior` is the source's position in the default table. A cell with no
evidence therefore reproduces the default exactly, a source shown once and
picked once barely moves, and a source that keeps winning climbs past sources
ranked above it. A source never shown keeps its default position instead of
falling to last. Below `MIN_OBSERVATIONS` (5) a cell is **not written at all** —
a ranking derived from one or two answers is worse than the reasoned default.

Raw choices are kept in the `elicitation` table rather than only the derived
ranking, so a better derivation can be re-run later without asking anything
twice — the same reason the pipeline keeps `field_candidate` (§11).

Verified end to end: 8 answers against `electronic`/`genre` moved the table from
`beatport → discogs → musicbrainz → essentia` to
`beatport → itunes → musicbrainz → discogs → essentia`, and flipped that
track's arbitrated genre from `Progressive House` to `Dance`. `beatport` held
first place throughout despite never being offered, which is the prior working.

## 10. format probe — results

**Outcome: AIFF, not FLAC.** Rekordbox 7 renders every required field from
AIFF/ID3v2.4. FLAC silently drops five, two of which (album artist, release
date) are on the must-have list. Keys were verified uppercase on disk, and 3–4
spellings were tried per failing field, so this is reader coverage, not a
key-naming mistake. AIFF also plays on every CDJ generation; FLAC requires
NXS2 or newer. Cost of the reversal: 79.4 GB instead of 57.7 GB.

### confirmed ID3v2.4 frame map (Rekordbox 7)

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

### confirmed by screenshot

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

### the file is written by more than one tool

Rekordbox and Serato both write into the same ID3 tag we do. Two consequences,
both measured on real files.

**Frames this project does not own are preserved across a rewrite.** Serato
stores its beatgrid and cue points in `GEOB` frames — `Serato BeatGrid`,
`Serato Markers2`, `Serato Autotags`, `Serato Overview`. A delete-then-write
destroyed all four on a real library file. `tag.write` now carries every
unowned frame across, so `music retag` cannot cost you an analysis. Owned
frames are `FRAME_MAP` plus `COMM` and `APIC`; everything else belongs to
another tool.

**Do not let Rekordbox write its analysed key back to the file.** Rekordbox has
a setting for this. Leaving it off keeps the *discovered* key — from a source
that knows the release — instead of overwriting it with an analysis of a lossy
transcode. Turning it on makes Rekordbox and this pipeline fight over `TKEY`,
and the last writer wins.

BPM is the exception and needs no protection: Rekordbox recomputes it on import
regardless (§10), and beatgrids get adjusted by hand anyway, so `TBPM` is
advisory in both directions.

### flac key map (recorded for a possible space-constrained USB build)

`REMIXER` (not MIXARTIST) · `LABEL` · `INITIALKEY` (not KEY) · `BPM` (not
TEMPO) · `DATE` · `GROUPING` · comment: Rekordbox reads `COMMENT`, Serato reads
`DESCRIPTION` — write both.

### capacity note

AIFF library ≈ 79.4 GB against a 64 GB USB: ~80% fits. Accepted. Fallback if it
bites: AIFF master library, FLAC/320k subset generated for the stick.

## 11. data model

SQLite. Audio files are a **projection** of this; re-tagging never requires
re-downloading or re-resolving.

```sql
-- Immutable record of one acquired audio file.
CREATE TABLE source_file (
  id            INTEGER PRIMARY KEY,
  origin        TEXT NOT NULL,          -- 'youtube' | 'local'
  video_id      TEXT UNIQUE,            -- youtube only; pre-download dedup key
  local_path    TEXT,                   -- local ingest only
  staging_path  TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  duration_s    REAL NOT NULL,
  codec TEXT, bitrate INTEGER, sample_rate INTEGER, itag TEXT,
  channel       TEXT,                   -- '- Topic' => art track
  description   TEXT,                   -- 'Provided to YouTube by' => art track
  raw_tags      TEXT,                   -- JSON, as acquired
  acquired_at   TEXT NOT NULL
);

-- One logical track. Carries pipeline state; `stage` is the resume marker.
CREATE TABLE track (
  id                  INTEGER PRIMARY KEY,
  source_file_id      INTEGER NOT NULL REFERENCES source_file(id),
  stage               TEXT NOT NULL,    -- normalise|classify|resolve|arbitrate|transcode|tag|publish
  status              TEXT NOT NULL,    -- pending|auto|review|published|failed|skipped
  genre_family        TEXT,             -- routes precedence
  genre_style         TEXT,             -- full 'Genre---Style', diagnostic only; never tagged
  is_video_rip        INTEGER DEFAULT 0,
  identity_confidence REAL,
  norm_artist TEXT, norm_title TEXT,    -- post-normalisation, what we query with
  chromaprint TEXT, acoustid TEXT, isrc TEXT, mb_recording_id TEXT,
  published_path      TEXT,
  updated_at          TEXT NOT NULL
);

-- Every value every source offered. Never overwritten; arbitration reads it.
CREATE TABLE field_candidate (
  id          INTEGER PRIMARY KEY,
  track_id    INTEGER NOT NULL REFERENCES track(id),
  field       TEXT NOT NULL,
  value       TEXT,
  source      TEXT NOT NULL,            -- musicbrainz|discogs|spotify|itunes|essentia|beatport|manual
  confidence  REAL,
  fetched_at  TEXT NOT NULL
);
CREATE INDEX idx_cand ON field_candidate(track_id, field);

-- Arbitration output: what actually gets written to the file.
CREATE TABLE resolved_field (
  track_id   INTEGER NOT NULL REFERENCES track(id),
  field      TEXT NOT NULL,
  value      TEXT,
  source     TEXT,
  decided_by TEXT NOT NULL,             -- precedence|manual|url_override
  PRIMARY KEY (track_id, field)
);

-- The elicitation exercise fills this in; one row per cell.
CREATE TABLE precedence (
  genre_family TEXT NOT NULL, field TEXT NOT NULL,
  rank INTEGER NOT NULL, source TEXT NOT NULL,
  PRIMARY KEY (genre_family, field, rank)
);

CREATE TABLE review_queue (
  track_id   INTEGER PRIMARY KEY REFERENCES track(id),
  reason     TEXT NOT NULL,             -- low_confidence|no_match|video_rip|duplicate
  created_at TEXT NOT NULL, resolved_at TEXT
);

-- Persistent HTTP cache. Re-running resolution must not re-hit the network.
CREATE TABLE api_cache (
  key TEXT PRIMARY KEY, source TEXT NOT NULL,
  response TEXT NOT NULL, fetched_at TEXT NOT NULL
);
```

`field_candidate` is append-only on purpose: when a tag looks wrong six months
from now, the full set of what every source said is still there to inspect.

## 12. confidence and arbitration

**Identity confidence gates entry, not ranking.** One threshold, one ordered
list. If a field is wrong you can point at exactly which source supplied it.

| Identity evidence | Confidence |
|---|---|
| Manual URL override | 1.00 |
| ISRC exact match | 0.98 |
| AcoustID ≥ 0.90 **and** duration within 3 s | 0.95 |
| Text match: score ≥ 90, duration within 3 s, no rival within 2 pts | 0.85 |
| Text match, duration mismatch **or** a close rival | 0.50 |
| No match | 0.00 |

- **≥ 0.80** → auto-accept. Candidates enter arbitration; precedence decides.
- **< 0.80** → nothing is written. Track goes to `review_queue` and is **not
  published**. An unresolved track never silently enters the library.

Version-variant guard: penalise a candidate whose title adds `(instrumental)`,
`(sped up)`, `(slowed)`, `(live)`, or `(radio edit)` when the query did not ask
for it. Measured need — a text match returned `SMASH! (instrumental)` at full
confidence with matching duration (§4).

### arbitration accuracy — first hundred-track run

The first real run published **83 tracks, 12 of them the wrong song**, every
one at confidence 0.95. Measured 2026-09-11; the failures were not random.

**Every failure came through the AcoustID route.** `parse_response` took
`recordings[0]` from the response, and an AcoustID entry lists many recordings
in no meaningful order. A wrong one was then described faithfully by
MusicBrainz — and MusicBrainz ranks first for pop, so it won `artist` and
`title` outright:

```
Morgan Wallen - Last Night
  itunes       Morgan Wallen - Last Night        (correct)
  spotify      Morgan Wallen - Last Night        (correct)
  musicbrainz  Metro Station - California        (wins on precedence)
```

**The fix is that agreement outweighs precedence for every factual field**, not
just the album group, where the rule already existed and had been measured to
work. Three tiers, tried in order, each only when the previous finds no two
sources agreeing:

1. exact normalised value
2. version qualifiers stripped — `Hey, Soul Sister (Country Mix)` and
   `Hey, Soul Sister` are the same song, and splitting them let an unrelated
   MusicBrainz title win. A `feat.` clause is never stripped here: it names the
   same recording more completely, not a different one
3. core only — parenthetical suffixes gone, or for `artist` the primary name
   alone, so `JAY-Z & Kanye West` and `JAY-Z` share a core. Within a core the
   fullest credit wins

`genre` and `artwork_url` are exempt: sources disagree on granularity by design
there, and which granularity is wanted is what elicitation calibrates (§9).

**A second bug hid behind the first.** `_best_album_source` tests each source's
album credit against the track artist to spot compilations, but was reading an
arbitrary artist candidate rather than the arbitrated one. Whenever a bad
source won `artist`, every *correct* album looked like a compilation and was
discarded — so fixing artist alone left albums wrong (`Last Night` on
`Metro Station`, `Drive By` on `Kidz Bop 22`). The artist is now decided first
and passed in.

Result, re-arbitrated offline from stored candidates with no re-download:

| | before | after |
|---|---|---|
| wrong artist | 12 | 0 |
| wrong album | 12 | 0 |
| artists rescued | — | 12 |
| regressions | — | 0 |

Remaining artist differences are all correct normalisations of a channel name
(`jayseanworldwide` → `Jay Sean`, `push baby` → `Rixton`).

**`music retag --rearbitrate` is the lever this pays for.** Arbitration re-runs
from `field_candidate`, which is append-only and already holds every value
every source offered, so a resolver fix reaches the whole library without
re-downloading or re-querying anything. Manual edits survive untouched. The
83-track library above was corrected in seconds this way.

**Album editions group without brackets too.** Guetta ships `Nothing But the
Beat`, `Nothing but the Beat 2.0` and `Nothing But the Beat Ultimate` with no
parentheses anywhere; ungrouped they are three albums of one source each, so
the edition a track landed on was decided by source order — one track got 2.0
and another got Ultimate off the same record. `ultimate` joins `DELUXE_WORDS`,
a trailing `N.N` counts as a reissue, and a trailing bare integer never does
(`Kidz Bop 22` and `Kidz Bop 21` are different records).

**A compilation is no longer used as a last resort.** `Down` was published as
track 1 of `Ultra Dance 11` credited to DJ Enferno — the only source that
answered was MusicBrainz, and it answered with the licensing compilation. When
every source offers only a compilation the album fields are now dropped: an
empty album is visibly incomplete and gets fixed in review, a wrong one
propagates into the filename, the folder and both DJ apps.

### measured outcome, and its cost to the review budget

Re-resolving the first run's 106 tracks with all of the above:

| | before | after |
|---|---|---|
| published as the wrong song | 14 | 0 |
| covers reaching the library silently | 4 | 0 |
| auto-accept rate | 95% | **75.5%** |
| review queue | 20 | 26 |

Cover detection found two failures that manual inspection had missed —
`Call Me Maybe` credited to Kidz Bop and `FourFiveSeconds` to Elliot Rhoads —
so the original damage was 14 of 83, not the 12 found by eye.

**This breaks the §9 budget as written.** That budget assumes ≥84% auto-accept;
at 75.5% the review queue over 2,329 tracks is roughly 570 items, about 5.5
hours at the assumed 35 s each. The previous 95% was not real — one track in
six of those was wrong — so the honest position is that the budget was never
being met, only unmeasured.

**Corroboration restores it.** A suspect fingerprint is not the same as an
unknown identity: when two enrichment sources independently agree on artist and
title *and* that agreement matches the query, the identity is settled and
confidence is raised to `CORROBORATED` (0.90). Both conditions are required —
without the second, two catalogues confidently describing the same wrong song
would confirm each other.

**Corroboration cannot lift a doubt about the audio.** A fingerprint is
*acoustic* evidence about this file; catalogue agreement is *bibliographic*
evidence about the song. Two catalogues confirming that Taylor Swift recorded
the track does not make this file her recording of it. So `variant_mismatch`
and `artist_unrelated` both veto corroboration, and are applied last.

This distinction was found by measurement, not reasoning. The first version let
corroboration lift everything, which took auto-accept to 90.6% — and silently
returned three covers to auto-accept, where they would have been published
under the original artist's name.

Final, over the same 106 tracks:

| | first run | now |
|---|---|---|
| published as the wrong song | 14 | 0 |
| covers reaching the library | 6 | 0 |
| auto-accept | 95% (false) | **86.8%** |
| review queue | 20 | 14 |

The 14 are all real: six covers, four duration mismatches, three version
mismatches, and `push baby`/Rixton — the one known false positive, a renamed
band. §9's budget needs ≥84%, so 86.8% holds.

### the cover belongs to the release, not to a precedence list

Artwork was resolved by its own precedence, independently of the album group.
MusicBrainz carries no images, so whenever it won the album — most of the time
for pop — the cover fell to iTunes, showing the sleeve of whichever release
*iTunes* had matched. Every text field was correct and the picture was from
another record: `Body & Soul` tagged to "The Juicebox" with the cover of "The
Juice, Vol. II"; `Outta My Head` tagged to "Free Spirit" with a cover by an
unrelated artist. **30 of 81 tracks** carried a cover from a different release.

Excluding `artwork_url` from agreement was wrong, and the reasoning behind it —
"several covers are all genuinely correct" — was wrong too. Genre is a matter
of granularity; a cover is the cover **of one release**. It is now chosen by
release:

1. the source that supplied the album, if it offers artwork at all
2. any source whose own album is the same release, editions included
3. precedence, last, so a track never loses its art entirely

That alone took wrong-release covers from 30 to 13. The remaining 13 were all
cases where MusicBrainz correctly chose the *album* while iTunes and Spotify
offered the *single* — `Icarus II` against `June - Single` — so no source held
the right cover at all.

**iTunes' release-type suffix is not a different record.** iTunes writes
`June - Single` and `YES - EP` where Spotify and MusicBrainz write the bare
title. Left unmerged the two look like separate releases, so iTunes was
excluded from the cover choice and a 640x640 Spotify image won over a
1200x1200 one *of the same artwork*. 12 of 120 tracks were losing quality this
way. `_album_key` now strips the suffix, which requires the dash — an album
genuinely called "The EP" is untouched, and `Blonde` still does not match
`Pink + White - Single`.

After the fix, an audit of every remaining Spotify cover: **53 are releases
iTunes genuinely did not match** (`channel ORANGE` against iTunes'
`Point Blank`, `Planet Pit (Deluxe)` against `Ultra Dance 13`) and **7 are
tracks iTunes offered no cover for at all**. Zero are the same release, so the
quality ordering is now doing everything it can.

**Release-accuracy is enforced first, then quality.** Every source that named
the album we tagged is equally correct, so among *those* the ranking is free to
be about image quality. Measured 2026-09-11: iTunes serves 1200x1200 at
294-546 KB, Spotify 640x640 at 126-180 KB — about four times the pixel area —
so the artwork table reads `itunes, spotify, musicbrainz`. That order is only
ever consulted among sources that agree on the release; a correct smaller cover
still beats a larger wrong one, which is the entire point.

**MusicBrainz supplies its own artwork now**, from the Cover Art
Archive, for the exact release the album fields came from. It ranks last on
quality — the scans are user-contributed and vary — and exists for the case
where nothing else has the release at all. Availability is
checked and cached rather than assumed, so a missing cover leaves the field to
iTunes instead of emitting a URL that 404s at publish time. Measured over 14
real releases: 13 had art on the release itself, and the one that did not
(`channel ORANGE`) had it on the release group, so release-then-group gave
complete coverage.

### features come from the join phrase, not from the album artist

`_credit_name` read only the first entry of a MusicBrainz artist-credit, so
everything after it was discarded: `Neverender` was tagged `Justice` with no
sign of Tame Impala anywhere. MusicBrainz records the join phrase between each
name, which is exactly why §7 ranks it first for credits — iTunes flattens the
same credit to `Justice & Tame Impala` and Spotify drops the guest entirely.

`split_credit` now reads the whole credit: names joined by `feat.` become
featured artists and move into the title in house style; names joined by `&`
or `,` are collaborators and stay on the artist line.

An earlier version inferred feature-ness instead, treating any artist beyond
the *album artist* as a guest. **That was wrong, and the user caught it: a
collaboration can appear on an album by one person.** `Neverender` is on
*Hyperdrama*, a Justice album, and is still a Justice/Tame Impala
collaboration — MusicBrainz joins the two with `&`, not `feat.`, and it is
right. The catalogue's own distinction is the authority; inferring one from
adjacent fields is not.

### rejecting a track has to stick

A hundred-track playlist brings in tracks that will never be played, so the
review ui can delete one. Both copies go — the published AIFF and the staging
download — because reclaiming the space is the reason for doing it.

**The `source_file` row is deliberately kept.** `already_have` is checked
before downloading (§8), so that row is a tombstone: re-ingesting the playlist
the track came from skips it instead of fetching it again. Deleting the row
would make the rejection last exactly until the next run of the same playlist,
which is the case it exists for.

The track keeps a `skipped` status and appears under its own filter, so a
rejection reads as a decision rather than as a track that vanished. Deleting
twice is harmless.

The control is two-step — `Delete` then `Really delete?` — rather than a
browser dialog. A dialog blocks the page, and this removes real audio.

### the library is flat

Nesting was tried two ways and both lost to the data.

**By genre family** it asked one value to do two jobs. `genre_family` exists to
select a precedence table, is computed once before any metadata is contested,
and is never updated — so it was simultaneously the most *stable* key available
and the one with the weakest claim to being right. Correcting a genre could not
move the file, and making it move files would relocate tracks under rekordbox
and serato, which track by path and lose cue points when a path changes.

**By artist** collaborations shatter a performer's catalogue. Measured on 120
tracks: Arijit Singh appears in **ten** distinct credits, including both
`Antara Mitra & Arijit Singh` and `Arijit Singh & Antara Mitra` — two
directories for one pairing.

**By album** the tree is barely a tree: 120 tracks produced **105 distinct
albums, 94 of them holding a single track**. That is the shape of a DJ library,
where tracks are collected individually, not a record collection.

So: `library/<Artist> - <Title>.aiff`, one directory. The path depends only on
the two fields least likely to be wrong, organisation happens inside the DJ
software where the user actually works, and the resolver can keep improving
without moving files underneath it. Migrating the existing library moved 120
files and removed 32 directories.

Empty directories are swept after a retag pass. On macOS this needs
`.DS_Store` treated as absence — Finder writes one into every directory it
displays, so a folder emptied of music is essentially never empty on disk, and
a naive `rmdir` prunes nothing at all.

### the hybrid title, and how well it reproduces a human

The title is assembled from two sources rather than taken from one: **Spotify
supplies the base** — it wins `title` outside electronic — and **MusicBrainz
supplies the guests**, being the only source that distinguishes a feature from
a collaboration. House style is then applied on the way to the file: `ft.` in
parentheses, remixes in brackets, ASCII quotes, contractions repaired, film
provenance removed.

Measured against the strongest available benchmark — the titles the user had
already corrected by hand — the pipeline now reproduces **11 of 12**, up from 6
before this work. That is the honest test: not whether the output looks
plausible, but whether it matches what a person who knows the music typed.

The one remaining difference is not a formatting problem. Khalid's `Eleven`
resolves to the solo cut because AcoustID matched the solo recording, and
MusicBrainz, iTunes and Spotify all describe it correctly — three sources agree
on the wrong *recording*. Nothing in title assembly can fix that; a pasted link
settles it in seconds.

### film provenance is not a title

Indian film catalogues append the film to the track name: `Jag Ghoomeya (From
"Sultan")`, `Ghungroo (From "War")`. 11 of 120 tracks arrived this way and every
one had been stripped by hand. The film is already carried by the album field,
so the suffix is removed in `canonical_title`.

The pattern is deliberately narrow — it must name a *work*, either quoted or
introduced by "the motion picture / film / series / soundtrack". A blanket
"trailing bracket starting with From" rule also eats `Title (From Another
Angle)`, and a missed strip is cosmetic where a wrong one destroys a real
title.

### the guest outlives the title that dropped it

Spotify leads `title` outside electronic and routinely omits the featured
credit; MusicBrainz keeps it but no longer wins the field. `Time of Our Lives
(feat. Ne-Yo)` therefore resolved to `Time of Our Lives`, with Ne-Yo appearing
nowhere on the tag.

MusicBrainz states who is featured in two places — the join phrase between
credited names, and the title's own `(feat. ...)` clause — and it is the only
source that distinguishes a guest from a collaborator at all. Both are now read
into a `featured_artists` candidate, and arbitration carries any missing name
onto whichever title won.

**This is not the album-artist inference that was tried and removed.** That
guessed feature-ness from adjacent fields and mislabelled a collaboration on a
solo album; this carries an *explicit statement* onto a title chosen elsewhere.
A name already in the title, or already on the artist line, is never added —
so `Pitbull, Ne-Yo, Afrojack & Nayer` keeps its collaborators on the artist
line and gains nothing in the title.

The division of labour that results:

| | artist | who is featured |
|---|---|---|
| **world** | MusicBrainz — the singer, not the music director | MusicBrainz |
| **everything else** | Spotify — the primary credit | MusicBrainz |

Spotify decides *who the act is*; MusicBrainz decides *who is a guest*. Each
source answers the question it is actually good at.

### every field, always

The review ui listed only fields that some source had offered, so `mix_name`
appeared on one track and was absent from the next — and a field that is not
shown cannot be typed into, which is exactly when you need it.

All 21 editable fields are now shown on every track, grouped (track, release,
classification, credits, notes, artwork) because twenty undifferentiated rows
scan worse than five groups. Empty ones are dimmed and dashed: available, not
missing.

**They fit in two columns rather than being cut.** Measured: 21 fields stack to
1052px in one column and overflow a 1201px viewport by 281px. Dropping
`grouping` and `comment` — the two obvious candidates — recovers about 112px
and still overflows by 169px, so it would have cost two working fields without
solving the problem. `columns: 30rem 2` halves the height, keeps each group
whole (`break-inside: avoid`), and falls back to one column below ~62rem.

The fixed outer columns are what keep every input the same width, so they are
sized to the longest real label and the longest real provenance string rather
than padded: 7rem and 8.25rem, which leaves the value 230px instead of 162px in
a two-column pane, with no provenance text clipped.

Below 34rem the row stacks entirely — at a 460px pane the three-column grid
squeezed the input to 68px, which is narrower than most of the values going
into it.

The list is **derived from the tag writer and served by the api**, not
hand-written in the ui. It had already drifted twice:

- `grouping` and `original_artist` are in `FRAME_MAP` and were never shown
- `comment` is writable but *not* in `FRAME_MAP` — `build` handles it
  specially — so deriving from `FRAME_MAP` alone still missed it, and §10
  measured it rendering in **both** Rekordbox and Serato

Two tests hold the two halves: everything writable is offered, and nothing
offered is unwritable.

### the ui shows what will be written

House style is applied at publish time, so `resolved_field` keeps the literal
string a catalogue supplied — `feat.`, a curly apostrophe — while the file gets
`ft.` and an ASCII one. The review ui showed the database, which made it look
as though the style had never been applied. Verified on disk: every published
tag reads `ft.`, so only the display was wrong. Resolved rows now carry a
`display` value in house style, and that is what the ui shows and edits.

### repairing what a source mangled

Spotify title-cases hard enough to produce `I’Ll Be Waiting` and
`Corners Of My Mind`. `I'Ll` is never correct English, so it is repaired on the
way out rather than fought over in precedence — no source ranking makes a
mangled string right.

The contraction suffixes are listed explicitly (`ll`, `re`, `ve`, `s`, `t`,
`d`, `m`, `n`) because a blanket "lowercase after an apostrophe" rule destroys
names: `O'Brien`, `D'Angelo`, `O'Neal`, `L'Amour`. That regression appeared in
the first version and was caught by running the examples.

`Corners Of My Mind` is **not** repaired. Lower-casing function words inside a
title is a defensible convention but it would also rewrite `Of Monsters and
Men` and `The Man Who Sold The World`, so it is left alone pending a decision.

### saved is not written

The ui had one notion of success and the system has two: a change is *recorded*
in the database the moment it is made, and *written* to the file only when the
track is accepted or re-tagged. Every confirmation the ui gave was for the
first, which is how a stranded artwork edit looked exactly like an applied one
for a day and a half.

Motion cannot carry this distinction. A spring says "something moved"; it
cannot say which field was saved, or which of the two things happened. So the
ui now says it in words:

- picking a value → **"Saved title — not yet in the file"**, amber
- an **unwritten changes** line listing the fields still only in the database
- pressing Re-tag → **"Written to Arijit Singh - Kesariya.aiff — cover
  replaced"**, green, and the unwritten list clears

`accept` reports what reached the disk — the filename, whether the file was
renamed, whether the cover was actually replaced — rather than a bare `ok`.

### precedence, rebuilt around what each catalogue is good at

Reset 2026-09-12 after working through a real Bollywood-heavy library.

**Spotify leads every identity field outside electronic** — title, artist,
album, album artist, track and disc number, ISRC. It is the catalogue a user
checks a track against, and MusicBrainz, which led these before, has the
thinnest coverage of exactly the material that needed the most review.

**Beatport leads them inside electronic**, for the same reason in reverse: it
is the catalogue that music is released into, and often the only source that
knows a given version exists.

**iTunes takes genre everywhere**, including electronic. Its vocabulary is
coarse and DJ-shaped, and it is the only source that treats Bollywood as a
genre rather than smearing it across `Stage & Screen`, `Folk, World, & Country`
and `Pop`: **46 of 49 world tracks correct against 0 for the classifier.**
Beatport's and Discogs' finer sub-genres move to `genre_style`, kept beside it
rather than discarded.

**iTunes takes artwork**, measured at 1200x1200 against Spotify's 640x640,
still subject to matching the release we tagged.

**Label moves to iTunes and Spotify ahead of Discogs.** Discogs describes the
pressing it catalogued, which is frequently a reissue or a regional licensee
rather than the label that released the recording in hand.

Effect on the existing library: Spotify's wins rose from 280 fields to 728 and
MusicBrainz's fell from 635 to 308.

**Essentia is now scoped to routing only.** It never becomes a genre answer.
On 49 real Bollywood tracks it returned Laïkó, K-pop, Éntekhno and Flamenco —
nearest-neighbour guesses from a model with no class for the material — while
still winning the genre field on 8 tracks. It stays because it is the only
signal describing *this audio* rather than a catalogue's opinion of it, and it
genuinely beats iTunes at telling dance from pop, which iTunes flattens.

### regional identification is not classification

The ISRC country override existed because the classifier cannot recognise a
regional tradition. Two more signals of the same kind now sit beside it:

- **a catalogue's own genre** — `Bollywood`, `Telugu`, `Filmi`, `Qawwali`;
  this catches a track released under a non-regional ISRC
- **the label** — T-Series, Tips, Eros appeared on 19 Bollywood tracks and
  **zero** non-Bollywood ones. Global labels are deliberately excluded

This is load-bearing rather than cosmetic. Seven Bollywood tracks were routed
to `pop`, so the world override never applied to them — and once Spotify led
`artist`, those tracks started resolving to the composer instead of the singer.
Routing them correctly is what keeps the vocalist rule working. Twelve tracks
move into `world`.

**The classifier's top-level genre is now stored** as a `classifier_genre`
candidate. Only the style was kept, so the routing *input* was unrecoverable
and a family could never be recomputed offline the way arbitration can (§11).

### two things that were documented but not implemented

**Credentials in `config.toml` never worked.** `load()` read them from the
environment alone, so a user following the README and moving keys to
`~/.config/musicpipeline/config.toml` lost every optional source — each of
which degrades silently by design. Both paths work now, environment first.
This matters because `.env` is read from the *working directory*: the same
command run from another folder quietly loses its credentials.

**The nightly had no tests to run.** It selected `-m live`, nothing carried
that marker, pytest exited 5, and it had been failing since it was written.
The marker was also documented as excluded from ordinary runs and nothing
excluded it, so the first live test written would have broken every local run
and CI. Both fixed: `addopts = "-m 'not live'"`, and nine live tests covering
MusicBrainz join phrases, iTunes' Bollywood label, Cover Art Archive, Spotify
track lookup, Discogs, AcoustID and the essentia model. Credentialed sources
skip rather than fail, so the nightly is useful with no secrets configured.

### genre: top level, except when the top level is not a genre

Discogs styles are too fine to browse: 120 tracks produced **107 distinct
styles**. Top-level genres gave 8 buckets with usable sizes, so `genre` takes
the top level and the style is kept beside it as `genre_style`.

But Discogs mixes two kinds of category at the top level. Most name a *sound*
(`Electronic`, `Hip Hop`, `Funk / Soul`); a few name an *origin*. **"Stage &
Screen" means the music came from a film, a musical or a TV show** — it covered
17 Bollywood tracks and 7 orchestral soundtracks in the same bucket, which is
not a distinction any set is built on. `Folk, World, & Country` behaves the
same way.

For those, the style is the real genre, so `PROVENANCE_GENRES` yields to it:
`Stage & Screen`/`Bollywood` resolves to `Bollywood`, `Stage & Screen`/
`Soundtrack` to `Soundtrack`.

**Known residue:** 15 Bollywood tracks are filed by Discogs under the top-level
`Pop` with the style `Bollywood`, and this rule leaves them as `Pop` — the
three-way split of Bollywood across `Stage & Screen`, `Folk, World, & Country`
and `Pop` is narrowed, not closed.

### the server must not hold stale code

`music serve` ran a single in-process app, so a server started before a code
change kept executing the old one indefinitely. That is invisible from the
browser: the ui saved the artwork choice, the database recorded it, the
endpoint returned success, and the file never changed — because the running
process still held the previous `retag`. The server in question had been up for
six hours across the fix that would have made it work.

`serve` now reloads on source changes by default (`--no-reload` opts out).
Verified by injecting a route into a running server and reaching it without a
restart.

The wider lesson is the one this project keeps relearning: **a silent
success is worse than a failure.** The same shape appears in the pasted link
that was recorded and never read, and in `retag` preserving the cover it was
asked to replace.

### convention is not consensus

Indian film music credits the **music director** as the track artist. iTunes
returns `Mithoon & Arijit Singh` for "Sanam Re" and Spotify returns `Mithoon`;
both name the composer. MusicBrainz returns the singer, `Arijit Singh`.

The house convention is **vocalists on the artist line, the fuller credit on
album artist**, so MusicBrainz is right here and the `world` table now ranks it
first for `artist` — the one field where iTunes does not lead this family.
Coverage and credit convention are different questions, and §7 conflated them.

Precedence alone was not enough. iTunes and Spotify agree, so agreement handed
the field to the composer regardless of ranking. **Two catalogues following the
same convention and agreeing is not independent evidence — it is the same
convention counted twice**, so `CONVENTION_FIELDS` marks `artist` in `world` as
decided by precedence rather than by agreement. The exemption is scoped: in
every other family two agreeing sources still beat one ranked higher, which is
what rescued twelve tracks earlier.

**This exposed a second bug.** Compilation detection treats "an album artist
that is not the track artist" as the giveaway, which is sound where both name
the same act — but in film music they never do. With vocalists on the artist
line, every Bollywood album suddenly looked like a compilation and the whole
album group would have been discarded. That signal is now disabled for
families in `CONVENTION_FIELDS`, leaving the explicit various-artists credit as
the test. Measured: 49 world albums kept, 0 dropped, 19 artists corrected.

### retag has to carry the cover too

`retag` rebuilt every text frame from the database and then kept whatever
artwork the file already held, unconditionally — `fields.build_for` treats
`artwork_url` as a non-tag field, so `built.tags.artwork` was always empty and
the existing image was restored every time. A cover corrected in the review ui
was written to the database, reported as saved, and never reached the file.
From the user's side that is indistinguishable from the edit not having been
submitted at all.

Schema v4 adds `track.artwork_url`: the url whose image is actually embedded.
That is the only way to tell a changed cover from an unchanged one without
re-downloading every image on every retag. `retag` re-fetches when the
resolved url differs from the recorded one, and leaves the file alone
otherwise.

Measured on the real library the moment it worked: **112 of 113 covers
replaced**, every one an artwork decision that had been stranded in the
database.

`music retag` now also reports what it did — covers replaced, files renamed,
files missing — rather than a bare count. "I hit retag and I don't know if it
did what I wanted" is a reporting failure as much as a functional one.

### a pasted link is fetched, not filed

`POST /api/track/{id}/url` wrote an `override_url` row that **nothing ever
read**, returned `ok`, and changed nothing. §9 calls manual URL override a
budget lever — the thing that turns a ninety-second puzzle into ten seconds —
and it was inert for its whole existence.

The link is now fetched: a Spotify track id goes to `/v1/tracks/{id}`, a
MusicBrainz recording id to `recording/{mbid}`. Every field comes back written
as `url_override`, which outranks resolution and survives re-arbitration,
because the user has stated exactly which recording this is. Unsupported kinds
return **422 with the reason** rather than a silent success. The track keeps
its place in the review list with the stale reason cleared, since accepting it
is still the user's call.

### the PO token warning, and why it is not yet a problem

yt-dlp 2026.08.19 warns on every fetch:

```
web_music client https formats require a GVS PO Token which was not provided.
They will be skipped as they may yield HTTP Error 403.
```

`web_music` is the client §4 chose in order to get itag 141, so a skipped
format would drop the run to itag 140 at 128 kbps — below the bitrate floor,
and every track would fail.

It does not, because **a GVS PO Token is not required for YouTube Premium
subscribers** (yt-dlp PO-Token-Guide, read 2026-09-12). Measured the same day:
6 of 6 downloads returned itag 141 at 258 kbps. The warning is generic; the
exemption is real.

That exemption is outside our control, so `music doctor` now downloads one
short track and reports the itag and bitrate it actually received. A
pre-flight check is the right place for a dependency on somebody else's
policy — the alternative is discovering it three hours into a 2,329-track run.
`--offline` skips it.

If it ever does change, the options are a PO token provider plugin
(`bgutil-ytdlp-pot-provider`) or a client that needs no token (`android_vr`,
`web_embedded`), both at some cost to available formats.

### cookies are extracted once per run

`--cookies-from-browser chrome` was passed on every yt-dlp call, so the browser
cookie store was re-read — and on macOS the keychain unlocked — once per track.
Over 2,329 tracks that is 2,329 decryptions of the user's entire cookie store
for one authenticated session. Measured: 110 cookies, 0.15 s.

**Per run, not per process.** The first fix cached the jar in a module global
for the life of the process while its own docstring claimed the life of the
run — and `music serve` stays up for days, so a server started on Monday would
still be presenting Monday's cookies on Thursday. YouTube rotates session
cookies. `refresh_cookies()` is called at the start of every ingest, which is
the correct unit: cheap enough to be invisible, fresh enough to pick up
rotation between runs.

**Mid-run rotation is recovered from its own symptom.** A full-library run
takes hours, so cookies can rotate part way through. The signal is specific and
already named: the best available stream drops below the bitrate floor, because
YouTube is serving the unauthenticated formats. That triggers one refresh and
one retry — responding to what actually went wrong rather than guessing a
refresh interval. Exactly one retry: if fresh cookies do not restore the
premium formats the problem is the account or the browser session, and quietly
re-reading the keychain on a loop would hide it.

Extraction failure still falls back to per-call rather than losing
authentication.

### result selection — one scorer, three call sites

Every source was asked for several results and used only the first. iTunes and
Spotify each fetch five and kept `results[0]`; AcoustID links many recordings
in no meaningful order and kept `recordings[0]`. Re-scoring the 100 cached
iTunes responses from the first run found **13 where a better result had been
fetched and discarded unexamined**, almost all of one shape — position zero is
a remaster, live cut or re-recording, and the original is further down:

```
Train — Hey, Soul Sister      [0] (Country Mix)              [3] plain  <- correct
zayn — PILLOWTALK             [0] (The Living Room Session)  [1] plain  <- correct
Taylor Swift — Love Story     [0] (Taylor's Version)         [4] plain  <- correct
Justin Bieber — Beauty And A Beat
                              [0] Baby (feat. Ludacris)      [2] correct song
```

That had a second-order effect: it is *why* iTunes and Spotify kept disagreeing
on version suffixes, which split them into separate groups and let MusicBrainz
win the arbitration above. One cause, two symptoms.

`identify.match_score` now answers "which of these is the right one" for every
source:

```
0.7 * (0.7 * exact_title_ratio + 0.3 * core_title_ratio) + 0.3 * artist_ratio
  - 0.25 if the result carries a version designation the query did not ask for
  - 0.12 if it carries a VARIANT_WORD the query did not ask for
```

Three deliberate choices:

- **Title dominates, artist supports rather than gates.** The query artist is
  often a YouTube channel (`jayseanworldwide`, `push baby`, `iyazlive`), so a
  weak artist match must not veto a perfect title. Within one source's results
  the query is constant, so a low artist weight still ranks correctly.
- **An exact rendering outscores a matching core.** `Love Story` and
  `Love Story (Taylor's Version)` share a core and are different recordings.
- **The version penalty does not use `VARIANT_WORDS`.** That list feeds
  `confidence`, and adding "remix" to it would drop every legitimate remix in
  an electronic library below auto-accept. The penalty uses `strip_version`'s
  vocabulary instead, and never applies to a `feat.` clause — that names the
  same recording more completely, not a different one. Verified: a remix asked
  for and returned scores 1.00, and a plain query answered with a remix still
  scores 0.49, ranked lower but not excluded.

**The fingerprint path is gated, not just ranked.** `_by_fingerprint` probes
three linked MBIDs; it used to take the first whose *release* matched the
artist and otherwise keep `recording_ids[0]` unexamined. It now scores each
probed recording against the text identity (a matching release is worth +0.25,
still the strongest single signal) and **returns no match at all** when the
best scores below `PLAUSIBLE` (0.45). Measured on the first run: the
catastrophic mismatches scored 0.14–0.36, every correct match 0.58 or above.

This is what actually repairs `identity_confidence`. Arbitration rescued those
twelve tracks because two sources happened to be right; the confidence still
read 0.95, so the review queue never saw them, and a track with only one good
source would still have published silently wrong.

**Covers are handled by a separate signal, because title scoring cannot see
them.** A cover carries the right title and a stranger's name, so it clears
every threshold above. Re-resolving the first run's library with ranking alone
left two tracks still wrong at 0.95 — `Waiting for Love` credited to
"Die NotenDealer" and `We Are Never Ever Getting Back Together` to
"Luke Conard" — where the AcoustID entry linked only the cover. Arbitration
rescued both, but confidence stayed at auto-accept, so review never saw them.

`artist_is_unrelated` therefore checks the credited artist against the query
independently and caps ACOUSTID confidence at 0.50 when they share nothing. It
is a **confidence signal only, never a selection filter** — filtering on artist
would reject every track whose query artist is a channel name.

Containment either way counts as related, which is what makes it safe on real
data. Verified against every query/match pair from the first run:

```
flagged      Avicii / Die NotenDealer   Taylor Swift / Luke Conard
not flagged  jayseanworldwide / Jay Sean    iyazlive / Iyaz
             Queen Official / Queen         JAY-Z / Jay-Z
             kesha / Ke$ha                  will i am / will.i.am
             Black Eyed Peas / The Black Eyed Peas
```

One known false positive: `push baby` is Rixton, a renamed band, and it is
flagged. That costs one review item. Relaxing until a renamed band passes would
let covers through, and a cover published as the original is silent and
permanent — so the rule errs toward asking.

**Still open:** the cover case above, and a query artist that is an
unrecognisable channel name (`jayseanworldwide`) still narrows which sources
answer at all — `Down` was found by MusicBrainz alone, which is how it reached
a compilation.

## 13. operational concerns

**Credentials.** This repository is public. There is **no credential file**:
yt-dlp reads cookies from Chrome at runtime via `--cookies-from-browser`. A
`cookies.txt` would hold live Google session tokens — full account access, not
just YouTube — and `.gitignore` is inadequate protection against `git add -f`.
Runtime config lives at `~/.config/musicpipeline/config.toml`, **outside the
repo**; only `config.example.toml` is committed. A pre-commit secret scan guards
the rest.

**Rate limiting.** MusicBrainz 1 req/s (and it 503s rather than queuing — 9 of
30 requests failed before backoff was added), Discogs 60/min authenticated on a
rolling window, AcoustID 3/s. Every adapter: exponential backoff with jitter,
and a descriptive User-Agent (MusicBrainz blocks requests without one).

**Caching.** Every response lands in `api_cache`. Resolution will be re-run many
times while precedence is tuned; reruns must cost nothing.

**Resumability.** `track.stage` is the marker. A crash at track 1,800 of 2,329
resumes at 1,800.

**Rekordbox caches tags per path and will not re-read on reimport** (§10).
Operational rule: **tag before import.** Never retag a file already in the
collection without a Reload Tag.

**Originals.** Staging files are retained until a track reaches `published` and
is verified, then eligible for cleanup. The 16 GB of existing M4A files stay
untouched until the new library is verified end to end.

### distribution

**Local-first. Packaged so others can run it on their own machine** (Docker
image or `pipx`). **No cookies ever leave a user's machine, and no audio is
hosted.**

A hosted version would have to hold users' Google session tokens. Those are
*account*-scoped, not YouTube-scoped — a breach would leak someone else's Gmail
and Drive, not just their music. It would also turn a personal tool into a
service that downloads YouTube audio on other people's behalf, which carries
materially different legal exposure. Neither is worth the convenience.

§15's web ui is already a localhost app, so distribution is a packaging problem,
not a rearchitecture.

**Keep all database access behind a single module** so that adding a `user_id`
later is a mechanical migration. Do not build multi-tenancy now.

**Dependencies.** `yt-dlp` (needs a JS runtime — Deno — for some formats),
`ffmpeg`, `essentia-tensorflow` (cp314 wheels), `mutagen`, `chromaprint/fpcalc`.

### starting over

`music reset` deletes the library, the staging directory and the database. The
database is the source of truth and the audio is a regenerable projection
(§11) — but a reset also discards every manual edit and every calibration
answer, and those are **not** regenerable from anything. It therefore requires
typing `delete <n>` where `n` is the exact file count, prints what it is about
to destroy first, and reports how many manual edits and answers are at stake.
`--keep-staging` keeps the downloaded audio so a rebuild needs no re-download.

Rekordbox and Serato hold their own databases pointing at these paths; after a
reset their libraries reference files that no longer exist.

## 14. output layout

```
library/
  <genre-family>/
    <artist>/
      <Artist> - <Title> (ft. <Guest>) [<Remixer> Remix].aiff
```

### canonical naming

House style, applied identically to the `TIT2` tag and the filename so search
behaves the same in Finder, Rekordbox and Serato:

| Rule | Example |
|---|---|
| Features use **`ft.`**, never `feat.`, in parentheses | `Mood (ft. iann dior)` |
| Credited artists: commas, then `&` before the last | `David Guetta, Bebe Rexha & Brooks` |
| Two credited artists therefore read as `A & B` | `Lost Frequencies & Calum Scott` |
| Remixes and edits use **square brackets** | `Delilah [Tom Santa Remix]` |
| Both may co-occur, features first | `Title (ft. Guest) [Someone Remix]` |
| Mix/remix designation is never dropped | — |

> **Two different normalisations, do not conflate them.** §6.5 *query*
> normalisation strips everything down to bare artist/title so sources can be
> matched. This is *canonical output* formatting, applied after arbitration to
> whatever the winning source returned. One is for machines, one is for you.

**Liberal on read, strict on write.** Input tags are inconsistent — the
existing library uses `;` on 427 tracks, `&` on 50 and `,` on 46, sometimes
mixed in a single string (`Atif Aslam, Sunidhi Chauhan & Pritam`). Parsing
accepts all of them; output always uses the serial-comma form above.

The `A, B & C` rule is one rule, not two: with two artists it collapses to
`A & B` on its own. Order comes from the catalogue's artist credit, primary
first — never alphabetical. It covers **credited** artists only; featured
artists stay in the title as `(ft. X)` and are never merged in.

> Known limitation: an artist whose *name* contains a comma — "Tyler, The
> Creator" — makes the written form ambiguous to re-parse. There are none in
> the current library, and it does not matter in practice because the database
> is the source of truth and we never re-parse our own output. It would only
> surface via `music add` on an already-published file.

Filesystem-illegal characters (`/`, `:`) are replaced; the tag keeps the true
value. If two tracks collide on path, a ` (2)` suffix is appended and both are
flagged for review as probable duplicates.

Six families (§7). Filename keeps the mix/remix designation — losing it is
worse than having no tag, since that is how a DJ searches. Album-based
foldering is rejected: Bollywood tracks sit on soundtrack "albums" that do not
match how they would be browsed, and the same holds for compilations.

Genre-first foldering also makes USB subsetting a folder copy, which matters at
79.4 GB against a 64 GB stick (§10).

## 15. web ui

A local web app (localhost, no auth) is the only human interface. It replaces
the three separate surfaces earlier drafts implied — review queue, elicitation,
and manual override — with one tool.

### why it exists

**Auto-accept will be wrong sometimes.** §9 projects ~85% auto-accept at
98–99% precision, which means roughly **25–35 tracks land in the library with a
wrong field and no flag on them.** Confidence thresholds cannot fix this: a
confident wrong match is exactly the case that does not reach review. The only
real remedy is making correction cheap and always available, on any track, at
any time — not just on the ones the pipeline doubted.

### modes

| Mode | Purpose |
|---|---|
| **review queue** | Work through `review_queue`: low confidence, no match, video rip, duplicate. |
| **metadata editor** | Open *any* track — including published ones — and edit any field directly. |
| **url override** | Paste a Spotify/MusicBrainz/Discogs/Beatport link; identity resolves exactly. |
| **elicitation** | Calibration mode (§9): candidate values side by side, source order randomised, choices populate the `precedence` table. |

### requirements

- **Audio preview.** Non-negotiable for review — the common failure is a
  plausible-looking match that is the wrong recording. You have to hear it.
  The preview serves the **download source**, not the published AIFF. Measured
  with `canPlayType` in Chrome 2026-09-11:

  | media type | Chrome |
  |---|---|
  | `audio/mp4a-latm` (what `mimetypes` guesses for `.m4a`) | *(empty)* |
  | `audio/x-aiff`, `audio/aiff` | *(empty)* |
  | `audio/mp4` | `maybe` |
  | `audio/wav` | `maybe` |
  | `audio/flac` | `probably` |

  No browser decodes AIFF, and the guessed type for `.m4a` names a raw LATM
  stream rather than the MP4 container — so both the published file *and* the
  staging file failed to play, silently, with the player showing `0:00`. The
  source is also ~6x smaller (7 MB vs 40 MB). Once staging is cleared, a
  192 kbps AAC preview is encoded from the published file on first request and
  cached (2.5 s cold, instant after).
- **Provenance on every field.** Show which source supplied the value and what
  the alternatives were, read straight from `field_candidate`. A wrong tag
  should be one click from *why*.
- **Edits are sticky.** A manual edit writes `resolved_field.decided_by =
  'manual'` and is **never** overwritten by a later resolution run. Re-running
  the resolver must be safe forever; that property is what makes the DB-as-
  source-of-truth design worth having.
- **Editing re-tags the file.** Save updates the DB, then re-emits ID3 to the
  AIFF and renames/moves it if the canonical name changed (§14). Because
  Rekordbox caches tags per path (§10), the UI must surface a **Reload Tag**
  reminder whenever a published file is edited.
- **Bulk operations.** Multi-select for the predictable batch fixes — a whole
  mislabelled genre family, or a run of tracks from one bad playlist.
- **The pipeline's real output is shown, not summarised.** yt-dlp is given a
  `logger` and `progress_hooks`, so its own lines — cookie extraction, the
  `[jsc:deno]` JS challenge, `FixupM4a`, the negotiated itag — appear in a
  console in the ui. The live download percentage is **not** a log line: with
  `noprogress` off yt-dlp writes one several times a second and buries every
  real event, so the percentage is rewritten in place from the hook and the log
  keeps only discrete events. The log is fetched incrementally by sequence
  number, so a one-second poll stays cheap.
- **Counters are per stage, not just per track.** `done` alone cannot
  distinguish "downloading track 40" from "waiting on MusicBrainz for track
  40", and at a hundred tracks those feel completely different. The run reports
  `downloaded`, `analysed`, `resolved`, `published`, `queued`, `skipped` and
  `failed` separately, and writes `track.stage` so an interrupted run has a
  resume marker (§13).
- **Every text input has a keyboard exit.** An input keeps focus until
  something takes it away, and while it holds focus every navigation key is
  dead — `j` types a `j` into the title rather than moving down, silently
  corrupting the field. Enter commits and leaves; Escape reverts and leaves.
  Without this the documented keyboard flow works exactly once.
- **Acquisition belongs in the ui.** A YouTube or YouTube Music link is pasted
  into the queue pane and runs on a worker thread, with live progress; one run
  at a time, since two would race on the staging directory and double the
  request rate against every source. The link box in the *track* pane overrides
  identity and is a different thing — having only that one was read as the
  place to add tracks.
- **One audio element per mode, moved rather than rebuilt.** Re-rendering a
  pane used to construct a fresh `<audio>`; each discarded element holds its
  connection until collection, so a handful of renders exhausts Chrome's
  six-connections-per-host budget and every later request stalls while the
  server stays perfectly healthy. At ~600 calibration questions this is the
  difference between a working session and an unexplainable one.
- **Published tracks stay reachable.** The status filter defaults to `review`,
  but a published track must remain one click away, show where it was filed,
  and say that its primary action re-tags rather than publishes. An unselected
  filter is styled as a pressable control — muted text on a transparent ground
  reads as disabled, and made the published tracks look unavailable.

### non-requirements

Not a player, not a library browser, not a Rekordbox replacement. It is a
correction surface. Anything the pipeline can decide, it decides.

## 16. milestones

1. **M0** — format probe. ✅ **Done.** AIFF selected; frame map confirmed.
2. **M1** — schema (§11) + resumable stage runner.
3. **M2** — download stage: enumerate, pre-download dedup, quality gate.
   *M2a* — Essentia classification. No dependencies, so it lands early:
   genre-family routing must be sanity-checked on the ~105 Bollywood tracks
   **before** arbitration is built on top of it (§7).
4. **M3** — normalisation (§6.5) + resolution + confidence (§12).
   *Normalisation lands first; it is worth more than any source.*
5. **M4** — arbitration (classification lands in M2a).
6. **M5** — web ui (§15): review queue, metadata editor, url override, audio
   preview, provenance.
7. **M6** — elicitation mode → `precedence` table.
8. **M7** — transcode + tag + publish (§14).
9. **M8** — full run over the playlists.
10. **M9** — `music add` for the 7 Beatport WAVs and the SoundCloud track.

## 17. project structure

```
music/
  SPEC.md  CLAUDE.md  pyproject.toml  .editorconfig  lefthook.yml
  .github/workflows/
    checks.yml            lint · format · types · tests · secrets · spec
    nightly.yml           live checks (essentia, source adapters)
  src/music/
    cli.py                command surface (§18)
    db/                   THE single database module (§13)
      schema.sql  migrate.py  runner.py     runner = resumable stages
    normalise.py          pure. +31pp. no dependencies
    classify.py           essentia → genre family
    acquire/
      youtube.py  local.py  quality.py
    sources/
      base.py             adapter protocol + pydantic models
      musicbrainz.py  discogs.py  spotify.py  itunes.py  beatport.py
      cache.py  ratelimit.py
    identify.py           confidence scoring (§12); pure core, thin i/o
    arbitrate.py          pure. precedence[family][field]
    publish/
      transcode.py  tag.py  naming.py  layout.py
    web/
      app.py  static/
  tests/
    unit/  integration/  cassettes/  conftest.py
  tools/
    check_spec.py         cross-reference validator (used by checks.yml)
```

**The organising rule** (from §19): modules that cannot run in CI must contain
no logic worth testing. `sources/` is a thin fetch layer plus a pure parser;
`identify.py` is a pure scorer with a thin lookup around it. Decisions live in
pure functions; i/o stays dumb.

`src/music/db/` is the only module that touches SQLite, so adding a `user_id`
later is a single-module migration (§13).

## 18. commands

```
music ingest <playlist-url>      download + run the full pipeline
music add <path>...              local files (beatport wavs, soundcloud)
music add --url <link>           authoritative identity from spotify/mb/discogs
music resolve [--redo]           re-run resolution over existing tracks
music retag [--all | --id N]     re-emit tags from the database
music status                     counts by stage and status
music serve                      web ui (§15); `music review` aliases it
music db migrate
music doctor                     preflight: ffmpeg, yt-dlp, cookies, models
```

`retag` and `resolve --redo` are the payoff for database-as-source-of-truth: a
better resolver re-tags the library without re-downloading anything. Neither
ever overwrites a manual edit (§15).

`doctor` exists because most failures in this project are environmental —
expired cookies silently dropping to 130 kbps, a missing model, an ffmpeg
without the right codec. It verifies each and reports, before a long run does.

## 19. testing strategy

**Most of this pipeline cannot run in CI**, and that is a design constraint, not
an inconvenience:

| Stage | Why not |
|---|---|
| `acquire` | needs Premium cookies — account-scoped secrets, never in CI |
| `sources` | rate-limited, keyed, flaky |
| `classify` | ~120 MB of wheels + tensorflow + real audio |
| full runs | 79 GB of output |

The modules that *can* be tested in CI are exactly the ones where being wrong is
most expensive — `normalise`, confidence scoring, arbitration, naming. Hence the
rule in §17: push decisions into pure functions, keep i/o thin.

| Tier | Covers | Speed | In CI |
|---|---|---|---|
| unit | pure fns: normalise, confidence, arbitrate, naming | ms | ✅ |
| integration | db + stage runner; transcode/tag round-trip | seconds | ✅ |
| cassette | source adapters replayed against recorded responses | ms | ✅ |
| live | real yt-dlp, real apis, real essentia | minutes | ❌ `-m live` |

**Golden-file tests for `normalise`.** A table of `(raw artist, raw title) →
(clean, clean)`, seeded from real failures already observed: `LMFAOVEVO`,
`Calvin Harris - I Need Your Love (Official Video)`, `Burnie, Phoenix Ho`. A
regression here would not crash anything — it would quietly cost accuracy. Golden
files make it loud.

**Cassettes, not mocks, for sources.** Record each api's real response once,
commit the json, replay forever. A mock encodes what we *think* MusicBrainz
returns; a cassette encodes what it *did*.

**Tagging round-trip.** ffmpeg synthesizes the test audio — no binaries are
committed:

```bash
ffmpeg -f lavfi -i "sine=frequency=440:duration=2" -c:a aac -b:a 256k   # 56 KB
```

Transcode it, write all 18 ID3v2.4 frames, read back, assert every frame
survived. This catches the `TDRC`-silently-dropped class of bug, which has
already happened once (§10).

**Coverage.** 90% on the pure modules (`normalise`, `identify` scoring,
`arbitrate`, `naming`) — hard-fail. No target elsewhere: coverage over i/o glue
measures how much was mocked, not how correct it is. Source adapters are held to
a contract test each instead.

## 20. tooling and checks

| Tool | Role |
|---|---|
| **uv** | dependencies and venv |
| **ruff** | lint **and** format — replaces black, flake8, isort, pyupgrade |
| **mypy** | static types; strict on pure modules |
| **pydantic** | runtime validation at api boundaries |
| **pytest** | tests; `live` and `slow` markers |
| **lefthook** | git hooks |

**mypy and pydantic are not alternatives.** mypy reads code without running it
and cannot see data arriving from the network; pydantic validates that data at
runtime. Every source adapter returns a pydantic model, so an api that changes
shape fails loudly at the boundary instead of writing `None` into the library
three stages later.

**lefthook over pre-commit.** pre-commit's main value is managing isolated tool
environments — redundant here, since `uv` already pins every tool. lefthook just
runs `uv run ruff check` in parallel.

### style

**Google Python Style Guide, with 2-space indentation** (a deliberate deviation
— Google specifies 4). Verified: ruff's formatter honours `indent-width = 2`,
and `pydocstyle convention = "google"` is active. Note the convention enforces
that docstrings exist and that recognised sections are well-formed; it will not
reject a numpy-style docstring, so that stays a review matter.

Line length 88. Lowercase filenames, headings and prose (`CLAUDE.md`).
`.editorconfig` covers markdown, yaml and toml, which ruff does not.

### ci

Split by **trigger**, not by job type:

```
checks.yml   on: [push, pull_request]
  lint      ruff check + ruff format --check
  types     mypy
  test      pytest -m "not live" + coverage gate
  secrets   gitleaks
  spec      tools/check_spec.py

nightly.yml  on: schedule
  live      essentia + model download + source adapters against real apis
```

Five jobs sharing one trigger belong in one file; splitting them means five
copies of the bootstrap and five badges for a single gate. `nightly.yml` is
separate because its trigger genuinely differs.

**`secrets`** is not boilerplate: this repo is public and the entire cookie
design rests on no credential ever landing in it (§13). That deserves
enforcement, not a convention.

**`spec`** validates that every `§n` reference resolves and no known-stale claim
survives. Dangling references have shipped twice and a mangled heading once.

**`nightly/live`** catches an essentia break or a source-api change before a
multi-hour run does.

## 21. boundaries

### always

- **Measure before asserting.** Every claim in §4 cites its evidence. If it
  cannot be cited, mark it *projected*. Two decisions in this project were
  already reversed by measurement — FLAC as target format, and Opus lowpass.
- Tag **before** importing to Rekordbox; it caches per path (§10).
- Hard-fail below 256 kbps rather than silently accept a downgrade.
- Write both `TDRC` and `TDRL` — Rekordbox shows them in different columns.
- Preserve the mix/remix designation. Losing it is worse than no tag.
- Keep all database access inside `src/music/db/`.

### ask first

- Deleting or overwriting audio files.
- Rewriting git history.
- Changing the target format or the §10 frame map — it is measured, so changing
  it requires a new probe, not an opinion.
- Lowering any threshold: confidence, coverage, bitrate.
- Adding a source that is paid, gated, or against a provider's terms.

### never

- Commit cookies, tokens, or any credential. No exceptions, no `.gitignore`
  reliance.
- Send a user's cookies to a server (§13).
- Re-encode lossy → lossy.
- Overwrite a manual edit with resolver output (§15).
- Publish a track below the confidence threshold — unresolved tracks stay out of
  the library (§12).
- Add Claude as a commit co-author (`CLAUDE.md`).
