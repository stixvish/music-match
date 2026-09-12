# cp2 results

**measured: 61.5% auto-accept** (96 tracks, stratified across six families,
seed 42). reproduce with `uv run python tools/measure_cp2.py <library.jsonl>`.

```
auto         59  (61%)        electronic  11/16  (69%)
review       30  (31%)        hip-hop     12/16  (75%)
no_match      7  ( 7%)        other        7/16  (44%)
                              pop          9/16  (56%)
                              r&b-soul    12/16  (75%)
                              world        8/16  (50%)
```

gate as written in `tasks/plan.md`: **<65% → stop and rethink.**

## first reading was 32.3%, and that was my bug

47 of 64 failures were tracks like these:

```
Miley Cyrus     | We Can't Stop       score=100  gap=0  duration Δ=0.0s
Daft Punk       | One More Time       score=100  gap=0  duration Δ=-1.1s
Backstreet Boys | I Want It That Way  score=100  gap=0  duration Δ=-0.0s
```

perfect score, perfect duration, scored 0.50 — because another result also
scored 100. those "rivals" were **the same recording on a different release**,
not a different song.

this exact flaw was identified in the first 30-track sample ("score 100 vs 100
is usually the same recording on different releases") and then rebuilt into the
confidence scorer. fixing it moved the number 32.3% → 61.5%. that was a bug fix,
not tuning; no threshold was changed.

## where the remaining 37 failures are

```
rival_gap variants   24   perfect duration match, still flagged
no_search_hit         7   musicbrainz has nothing
duration_off          6   music-video rips with inflated durations
low_score alone       2
```

- the **6 duration_off** are video rips. phase 2 re-downloads art tracks and
  should convert most of these.
- the **7 no_search_hit** need a second source. only musicbrainz exists.
- the **24 rival_gap** cases mostly look like they *should* be confident. more
  scorer work is probably justified — but it must not be done by watching this
  number move.

## the threshold placement is the real problem

§9 projects ~85% from **five sources plus clean art-track audio**. cp2 runs with
**one source and un-redownloaded audio**, yet carries thresholds calibrated for
the finished system. 61.5% with musicbrainz alone is *above* the 55% baseline
and consistent with the projected path — it is an intermediate reading being
judged against an end-state bar.

that is a flaw in `tasks/plan.md`, not a result. the options are in the handover
note; this needs a human decision rather than another round of scorer changes.


## readings after each change

| stage | reading | change |
|---|---|---|
| text only, no normalisation | 55% | baseline |
| + normalisation (cp2 as first run) | 32.3% | scorer bug: same-recording rivals |
| + rival fix | 61.5% | bug fix, no threshold moved |
| + collaborator/bonus-track normalisation | 62.5% | `no_match` 7 → 5 |
| **+ acoustid fingerprinting** | **77.1%** | **above the cp4 proceed bar** |

acoustid rescued **30 of the 36** tracks musicbrainz could not auto-accept — an
83% rescue rate on failures, including bollywood, where text search is weakest.

still to come before cp4: discogs, spotify, itunes, url override, and audio
re-downloaded as art tracks (which should convert the remaining duration
mismatches).


## after all sources (seed 99, held out)

```
identity auto-accepted   24/30  (80%)

album / album_artist / artist / disc / isrc / date / title / track / year   97%
genre                                                                      93%
label                                                                      73%
bpm · key · composer · lyricist · mix_name · original_artist · remixer       0%
```

candidates by source: musicbrainz 253 · itunes 63 · discogs 44 · spotify 31.

**isrc is back at 97%.** option c discarded 1,755 existing isrcs; resolution
recovers them, so the cost of that decision has largely been repaid.

the zero-coverage fields are known and each has an owner:

- `bpm`, `key` — computed locally by essentia in phase 4, never fetched
- `composer`, `lyricist` — need musicbrainz work-level relationships
  (`inc=work-rels+artist-rels`), which the adapter does not request yet
- `remixer`, `mix_name`, `original_artist` — derivable from the title we
  already have; no extra request needed
- `grouping` — ours to fill, not a source's
