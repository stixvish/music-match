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
