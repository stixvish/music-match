# implementation plan

companion to `spec.md`. the spec says *what* and *why*; this says *in what
order* and *how we know it worked*.

## the departure from spec.md §16

**§16's milestones are horizontal** — build the schema, then the downloader,
then resolution, then tagging. that ordering defers every integration question
to the end. the first time a downloaded file meets the tagger would be M7, and
the first time 2,329 of them meet it would be M8.

**this plan slices vertically instead.** phase 0 builds a *walking skeleton*:
one track, one url, all the way to a tagged aiff in the library, with every
stage present but stubbed to its dumbest possible implementation. every phase
after that replaces a stub with the real thing, and **the pipeline never stops
working**.

§17's module layout is unaffected — that is code organisation, not build order.
this plan supersedes §16's sequencing only.

## risk ordering

two projections could invalidate the design. both are front-loaded.

| risk | if wrong | resolved at |
|---|---|---|
| stages do not compose | rework across every module | **cp1** (phase 0) |
| resolution accuracy < 85% | 5-hour review budget breaks (§9) | **cp2** (phase 1) |

cp2 is the important one. §9 projects ~85% auto-accept from a **measured 55%**
baseline, assuming clean audio plus four more sources. if the real number lands
at 65%, review becomes ~8 hours and either the budget or the precision target
has to move. that is a go/no-go, and it happens before any source adapter
beyond musicbrainz is written.

## dependency graph

```
db ──┬── acquire ──┐
     ├── sources ──┼── identify ──┐
     ├── normalise ┘              ├── arbitrate ── publish ── web ── cli
     └── classify ────────────────┘
```

`normalise` and `classify` have no dependencies. `db` blocks everything.

## phases

### phase 0 — walking skeleton  ·  one track, end to end
proves the stages compose before any of them is good.
stubs: normalise = passthrough · classify = `"other"` · resolve = first
musicbrainz hit · arbitrate = first candidate · tag = `TIT2`/`TPE1` only.

**cp1** — one real track lands in `library/` and displays correctly in **both**
rekordbox and serato. if this is painful, the architecture is wrong and it is
cheap to change now.

### phase 1 — normalise + accuracy spike  ·  de-risk the budget
`normalise` is dependency-free and worth +31pp (§4). it lands before anything
that touches a network.

**cp2 — go/no-go.** run resolution over a 100-track stratified sample and
measure high-confidence rate. **≥75% → proceed.** 65–75% → proceed, revise §9's
budget. **<65% → stop and rethink** before building five adapters on a broken
premise.

### phase 2 — acquisition is real
format chain, quality gate, playlist enumeration, pre-download dedup, art-track
preference.

**cp3** — a 20-track playlist ingests; every file ≥256 kbps; re-running
downloads nothing; a known music-video url is flagged.

### phase 3 — resolution is real
remaining adapters behind the §7 protocol, acoustid, url override, confidence.

**cp4** — accuracy re-measured on the same 100 tracks. expect movement toward
85%. cassettes exist for every adapter; ci is green without network.

### phase 4 — classify + arbitrate
**cp5 — bollywood gate.** genre routing checked on 30 of the ~105 bollywood
tracks (§7 flags this as the least reliable routing *and* the weakest source
coverage). if families are wrong there, fix routing before arbitration depends
on it.

### phase 5 — publish quality
all 18 frames, canonical naming (`ft.` / `[remix]`), layout, `retag`.

**cp6** — 20 tracks round-trip every frame; both apps show them; `retag` does
not clobber a manual edit.

### phase 6 — web ui
review queue, audio preview, metadata editor, sticky edits, elicitation.

**cp7 — budget reality check.** review 50 real items and measure
seconds-per-item against §9's assumed 35 s. this is the second half of the
budget question; cp2 measured how *many* items, this measures how *long* each
takes.

### phase 7 — the real run
elicitation session populates `precedence`, then the full run, then `music add`
for the 7 beatport wavs and the soundcloud track.

**cp8** — library complete; review queue drained; total manual time recorded
against the 5-hour budget.

## checkpoint protocol

a checkpoint is a stop. do not start the next phase until it passes.
each one is either a **measurement** (cp2, cp4, cp7, cp8) or a **human
verification in real software** (cp1, cp3, cp5, cp6). no checkpoint is "the
code looks right".

## pr policy

one pr per phase (`claude.md`). small commits within the branch.
