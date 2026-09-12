# CLAUDE.md

working conventions for this repo. **this file is about *how we work*.
`SPEC.md` is about *what we are building*.** keep them separate — decisions
about the product go in the spec, decisions about process go here.

## style

- **lowercase prose.** document headings are lowercase, and sentences need not
  start with a capital. this is a writing style, not a filesystem rule.
- **filenames follow convention, not the prose style.** root-level project docs
  are uppercase by convention (`SPEC.md`, `README.md`, `CLAUDE.md`); everything
  else follows its ecosystem (`pyproject.toml`, `tasks/todo.md`).
- **code follows its language's convention.** python is `snake_case` for
  functions and variables, `PascalCase` for classes — google style, 2-space
  indent (`SPEC.md` §20). never impose the prose style on identifiers.
- comments explain *why*, never *what*.

## commits

- **conventional commits, short.** `type(scope): subject`, lowercase,
  imperative, **under 70 characters**.
- **no body.** if the why matters, it belongs in `SPEC.md`, not a commit
  message. the spec is the record; commits are pointers.
- types: `feat` `fix` `docs` `style` `refactor` `perf` `test` `build` `ci`
  `chore` `revert`.
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
- **one pr per milestone or stage** (see `SPEC.md` §16). small commits within
  the branch; the pr is the unit of review.
- pr description: what changed, why, and what was verified.

## working agreements

- **measure, do not assume.** this project has already reversed two decisions
  that were confidently wrong on paper — flac as the target format, and opus
  having a lowpass. if a claim can be tested locally in under an hour, test it
  before writing it into the spec.
- **every claim in `SPEC.md` cites its evidence.** if it cannot be cited, mark
  it as projected.
- **never destroy another tool's data.** rekordbox and serato write into the
  same id3 tag we do; serato keeps beatgrids and cue points in `GEOB` frames.
  any code that rewrites a tag must preserve frames it does not own.
- **no secrets in the repo, ever.** no cookie files, no tokens. runtime config
  lives in `~/.config/musicpipeline/`. see `SPEC.md` §13.
- push back when an approach has a real problem; do not agree by default.

## repo layout

```
SPEC.md    what we are building, and the evidence for it
CLAUDE.md  this file — how we work
tasks/     plan.md (phases) and todo.md (tasks)
```
