---
name: probe
description: Compare what every metadata source says about the same tracks, and tune precedence.toml from the result. Use when choosing or revisiting which source should lead for a genre or a field, when a source's data quality seems to have shifted, or after adding a new source module.
---

# Probing metadata sources

`precedence.toml` decides which source is asked first for which field. It
is only worth what the evidence behind it is worth, so it gets tuned from
observed data — never from assumptions about which database "should" be
better.

This is a permanent tool, not a one-time setup step. Source data quality
shifts, APIs change what they return, and the library grows into new
genres. Re-run it when any of those happen.

## Running it

```bash
uv run music-match probe <files...>          # queries built from file tags
uv run music-match probe --artist X --title Y  # ad-hoc, no file needed
```

Useful flags: `--only discogs,spotify` to ask a subset, `--all-fields` to
include fields nothing answered, `--no-cache` to bypass cached responses.

**Pick the sample deliberately.** Ten well-chosen tracks beat a hundred
random ones. Cover the genres the library actually holds — check with
`music-match genre summary` — and make sure the sample includes:

- a **remix**, or `remixer` and `mix_name` never get exercised
- a **compilation or soundtrack** appearance, which is where sources
  disagree most about `album`
- something **obscure**, since every source handles hits well and that
  tells you nothing
- a **non-Western release** if the library has them, where coverage tends
  to collapse

## Reading the output

Two parts. The per-track section shows every source's value for every
field side by side. The coverage table at the end counts how often each
source supplied each field across the whole sample — that is the number
precedence is tuned on.

Read them together, because they answer different questions:

- **Coverage** answers "does this source ever have this field?" A source
  that returns an ISRC for one track in ten should not lead `isrc`,
  however good its other data is.
- **The per-track view** answers "is the value any good?" High coverage
  with wrong values is worse than no coverage, and only the side-by-side
  shows it. Watch `album` in particular: a source confidently returning a
  compilation or a soundtrack rather than the original release is a
  common failure that coverage alone scores as a success.

## Turning that into precedence.toml

Order each field's `order` list by coverage first, then by observed
quality, and only list a field override where a source is *meaningfully*
better or worse than the genre default — the file is easier to reason
about when it is short.

```toml
[genres.electronic]
order = ["discogs", "musicbrainz", "spotify"]

[genres.electronic.fields]
isrc = ["spotify", "musicbrainz"]     # because Spotify carries it and Discogs does not
```

Add a comment saying *what was observed*, not what was decided. "Spotify
first: 3/3 vs Discogs 0/3" survives being read in six months; "Spotify is
better" does not.

Leave a genre out entirely if the library has few tracks in it — it falls
back to `[genres.default]`, which is the right answer until there is
evidence to the contrary.

## What each source is for

Established by probing, and worth re-checking rather than trusting:

| Source | Reliably carries | Never carries |
|---|---|---|
| **discogs** | genre/style, `remixer`, `composer`, `mix_name`, label, catalogue number | `isrc`, `release_date` |
| **spotify** | `isrc`, full `release_date`, track/disc numbers, 640px art | credits of any kind, genre |
| **itunes** | `album`, `release_date`, track/disc numbers, coarse genre, 640px art | `isrc`, credits |
| **musicbrainz** | artist credits, `track_total` | art; `isrc` only sometimes, and it costs an extra request |

Discogs credits live on the **tracklist entry**, not the release, so a
remixer only appears once the right track is matched off the record.

## Caveats that will bite

- **Cached by default** (seven days, in `.music-match/http-cache/`). That
  is what makes re-running cheap, but it also means a probe can show you
  yesterday's data when you are specifically checking whether a source has
  changed. Use `--no-cache` for that.
- **Rate limits are real.** MusicBrainz allows one request a second and
  enforces it; Discogs allows sixty a minute. A large sample takes minutes,
  and that is the tool waiting, not hanging.
- **MusicBrainz ISRCs cost an extra request each**, so its ISRC coverage
  looks worse than its database is if that lookup is disabled.
- **The probe does no matching.** Every source's own top hit is reported
  as-is, so a bad row may mean the source ranked badly rather than that
  its data is poor. Judging *which* candidate is right is the matcher's
  job, deliberately kept separate.
