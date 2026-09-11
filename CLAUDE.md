# claude.md

working conventions for this repo. **this file is about *how we work*.
`spec.md` is about *what we are building*.** keep them separate — decisions
about the product go in the spec, decisions about process go here.

## style

- **lowercase.** filenames, headings, prose, identifiers. `spec.md`, not
  `SPEC.md`. the one exception is this file: claude code only reads
  `CLAUDE.md` in caps, so the name stays uppercase while the content does not.
- python: `snake_case`, no single-letter names outside comprehensions.
- comments explain *why*, never *what*.

## commits

- **conventional commits, short and sweet.** `type(scope): subject`, subject in
  lowercase, imperative, under ~60 chars. body only when the *why* is not
  obvious from the diff.
- types: `feat` `fix` `docs` `refactor` `test` `chore`.
- **never add claude as a co-author.** no `Co-Authored-By`, no
  `Generated with` trailers. these are the user's commits.
- small commits for small tasks. do not batch unrelated work.

```
feat(resolve): add musicbrainz text lookup
fix(tag): write TDRL so rekordbox shows release date
docs(spec): record opus vs aac spectral measurement
```

## branches and prs

- work on a branch, never commit directly to `master`.
- **one pr per milestone or stage** (see `spec.md` §16). small commits within
  the branch; the pr is the unit of review.
- pr description: what changed, why, and what was verified.

## working agreements

- **measure, do not assume.** this project has already reversed two decisions
  that were confidently wrong on paper — flac as the target format, and opus
  having a lowpass. if a claim can be tested locally in under an hour, test it
  before writing it into the spec.
- **every claim in `spec.md` cites its evidence.** if it cannot be cited, mark
  it as projected.
- **no secrets in the repo, ever.** no cookie files, no tokens. runtime config
  lives in `~/.config/musicpipeline/`. see `spec.md` §13.
- push back when an approach has a real problem; do not agree by default.

## repo layout

```
spec.md              what we are building, and the evidence for it
claude.md            this file — how we work
tools/format-probe/  reproducible proof for spec.md §10
```
