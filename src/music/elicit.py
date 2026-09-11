"""Calibrate per-(field x family) source precedence from the user's choices.

The built-in rankings in `arbitrate` are reasoned guesses. This measures the
real preference: show the values two or more sources offered for one field,
**without saying which source is which**, and record what gets picked
(SPEC.md §9).

Blindness is the point. A labelled comparison measures which source the user
trusts; an unlabelled one measures which value is actually better. Those two
differ, and only the second is worth writing into the precedence table.
"""

import random
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from music.arbitrate import DATE_FIELDS, precedence_for
from music.sources.base import ALBUM_GROUP, FieldCandidate

# Fields worth asking about: precedence decides them, AND the user can judge
# them by looking.
#
# Excluded on the first ground: `release_date` and `year` (`_earliest` decides
# those, precedence is never consulted), `bpm` and `key` (local analysis), and
# the album group's other three fields (they move with `album`, never alone).
#
# Excluded on the second: `isrc` and `catalog_number`. An identifier is right
# or wrong, not better or worse, and nobody can tell which by reading it.
# Asking would collect coin flips and write them in as preference.
ELICITABLE: tuple[str, ...] = (
  "title",
  "artist",
  "album",
  "genre",
  "label",
  "remixer",
  "mix_name",
  "original_artist",
  "composer",
  "lyricist",
  "artwork_url",
)

# Observations wanted per (family, field) before a cell stops being asked.
# Stratifying by cell rather than by track is what makes the session finite and
# evenly covered: 120 tracks drawn at random would ask `genre` for pop eighty
# times and never once for world.
TARGET_PER_CELL = 8

# Below this, a cell keeps the built-in default. A ranking derived from one or
# two answers is noise dressed as calibration.
MIN_OBSERVATIONS = 5

# Weight of the built-in ranking as a prior, in pseudo-observations. Evidence
# must accumulate before a cell moves off the default, and a source nobody was
# ever shown keeps its default position rather than falling to last.
PRIOR_WEIGHT = 3.0

# The album group resolves atomically (SPEC.md §7), so the choice is between
# whole groups; these are the labels the other three are shown under.
_GROUP_LABELS = {
  "album_artist": "album artist",
  "track_number": "track",
  "disc_number": "disc",
}


@dataclass(frozen=True)
class Option:
  """One distinct answer, and every source that offered it.

  Sources agreeing on a value share an option rather than appearing twice.
  Three identical strings presented as three choices is a trick question, and
  it costs the reviewer time the budget does not have.
  """

  value: str
  sources: tuple[str, ...]
  extra: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Question:
  """One blind comparison."""

  track_id: int
  family: str
  field: str
  options: tuple[Option, ...]
  title: str = ""
  artist: str = ""


@dataclass
class Tally:
  """Wins and appearances for one source within one cell."""

  wins: int = 0
  seen: int = 0


@dataclass(frozen=True)
class Observation:
  """A recorded choice. `chosen` is empty when the user saw no difference."""

  family: str
  field: str
  chosen: tuple[str, ...]
  offered: tuple[str, ...]


def _norm(text: str) -> str:
  return " ".join((text or "").casefold().split())


def build_question(
  track_id: int,
  family: str,
  field_name: str,
  candidates: Sequence[FieldCandidate],
  *,
  rng: random.Random | None = None,
  title: str = "",
  artist: str = "",
) -> Question | None:
  """Build one blind comparison, or None if there is nothing to compare.

  Args:
    track_id: Track the candidates belong to.
    family: Genre family, selecting the cell.
    field_name: Field being compared.
    candidates: Every candidate for this track, across all fields.
    rng: Source of shuffling, injectable for tests.
    title: Track title, for context in the UI.
    artist: Track artist, for context in the UI.

  Returns:
    A Question with at least two distinct options, else None.
  """
  if field_name not in ELICITABLE:
    return None
  rng = rng or random.Random()

  by_source: dict[str, dict[str, str]] = defaultdict(dict)
  for candidate in candidates:
    if candidate.value:
      by_source[candidate.source][candidate.field] = candidate.value

  # key -> (display value, extra rows, sources offering it)
  grouped: dict[str, tuple[str, tuple[tuple[str, str], ...], list[str]]] = {}
  for source, values in by_source.items():
    primary = values.get(field_name)
    if not primary:
      continue
    extra = (
      # ALBUM_GROUP drives membership and order so the two cannot drift; the
      # labels below are cosmetic, and a field added there still appears.
      tuple(
        (_GROUP_LABELS.get(f, f), values[f])
        for f in ALBUM_GROUP
        if f != "album" and values.get(f)
      )
      if field_name == "album"
      else ()
    )
    key = "|".join([_norm(primary), *(f"{k}={_norm(v)}" for k, v in extra)])
    if key in grouped:
      grouped[key][2].append(source)
    else:
      grouped[key] = (primary, extra, [source])

  if len(grouped) < 2:
    return None

  options = [
    Option(value=value, sources=tuple(sorted(sources)), extra=extra)
    for value, extra, sources in grouped.values()
  ]
  rng.shuffle(options)
  return Question(
    track_id=track_id,
    family=family,
    field=field_name,
    options=tuple(options),
    title=title,
    artist=artist,
  )


def tally(
  observations: Iterable[Observation],
) -> dict[tuple[str, str], dict[str, Tally]]:
  """Count wins and appearances per source, per cell.

  Args:
    observations: Recorded choices.

  Returns:
    `{(family, field): {source: Tally}}`.
  """
  cells: dict[tuple[str, str], dict[str, Tally]] = defaultdict(
    lambda: defaultdict(Tally)
  )
  for obs in observations:
    cell = cells[(obs.family, obs.field)]
    for source in obs.offered:
      cell[source].seen += 1
    for source in obs.chosen:
      cell[source].wins += 1
  return {key: dict(value) for key, value in cells.items()}


def _prior(source: str, default: Sequence[str]) -> float:
  """Score a source by its position in the built-in ranking, in (0, 1)."""
  if source not in default:
    return 0.0
  return 1.0 - default.index(source) / (len(default) + 1)


def rank_cell(
  counts: dict[str, Tally],
  default: Sequence[str],
  *,
  min_observations: int = MIN_OBSERVATIONS,
) -> tuple[str, ...]:
  """Rank one cell's sources from its tallies, shrunk toward the default.

  The score is a smoothed win rate whose prior is the source's position in the
  built-in ranking::

      (wins + PRIOR_WEIGHT * prior) / (appearances + PRIOR_WEIGHT)

  A cell with no evidence therefore reproduces the default exactly, a source
  shown once and picked once barely moves, and a source that keeps winning
  climbs past sources ranked above it. A source that was never shown keeps its
  default position instead of being dropped.

  Args:
    counts: Tallies for this cell.
    default: The built-in ranking.
    min_observations: Answers required before the cell is calibrated at all.

  Returns:
    Sources best first, or empty when the evidence is too thin to use.
  """
  answered = max((entry.seen for entry in counts.values()), default=0)
  if answered < min_observations:
    return ()

  sources = list(dict.fromkeys([*default, *counts]))
  scored = []
  for index, source in enumerate(sources):
    entry = counts.get(source, Tally())
    score = (entry.wins + PRIOR_WEIGHT * _prior(source, default)) / (
      entry.seen + PRIOR_WEIGHT
    )
    # ties fall back to the default order, which `index` already encodes
    scored.append((-score, index, source))
  return tuple(source for _, _, source in sorted(scored))


def derive(
  observations: Iterable[Observation],
  *,
  ranking=precedence_for,
  min_observations: int = MIN_OBSERVATIONS,
) -> dict[tuple[str, str], tuple[str, ...]]:
  """Turn recorded choices into precedence rows.

  Args:
    observations: Every recorded choice.
    ranking: `(family, field) -> sources`, the prior.
    min_observations: Answers required before a cell is written.

  Returns:
    `{(family, field): ranked sources}`, containing only calibrated cells.
  """
  out: dict[tuple[str, str], tuple[str, ...]] = {}
  for (family, field_name), counts in tally(observations).items():
    if field_name in DATE_FIELDS:
      continue
    ranked = rank_cell(
      counts, ranking(family, field_name), min_observations=min_observations
    )
    if ranked:
      out[(family, field_name)] = ranked
  return out


# --- database -------------------------------------------------------------


def record(conn: sqlite3.Connection, question: Question, chosen: Sequence[str]) -> None:
  """Store one answer. Append-only, like `field_candidate`.

  Keeping the raw choices rather than only the derived ranking means a better
  derivation can be re-run later without asking the user anything again — the
  same reason the pipeline keeps candidates (SPEC.md §11).

  Args:
    conn: Open connection.
    question: The question that was asked.
    chosen: Sources behind the chosen option; empty for "no preference".
  """
  offered = sorted({s for option in question.options for s in option.sources})
  conn.execute(
    "INSERT INTO elicitation (track_id, genre_family, field, chosen, offered)"
    " VALUES (?,?,?,?,?)",
    (
      question.track_id,
      question.family,
      question.field,
      ",".join(sorted(chosen)),
      ",".join(offered),
    ),
  )


def observations(conn: sqlite3.Connection) -> list[Observation]:
  """Read back every recorded choice.

  Args:
    conn: Open connection.

  Returns:
    Observations in the order they were made.
  """
  return [
    Observation(
      family=row["genre_family"],
      field=row["field"],
      chosen=tuple(s for s in row["chosen"].split(",") if s),
      offered=tuple(s for s in row["offered"].split(",") if s),
    )
    for row in conn.execute(
      "SELECT genre_family, field, chosen, offered FROM elicitation ORDER BY id"
    )
  ]


def cell_counts(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
  """How many answers each cell has so far.

  Args:
    conn: Open connection.

  Returns:
    `{(family, field): answers}`.
  """
  return {
    (row["genre_family"], row["field"]): row["n"]
    for row in conn.execute(
      "SELECT genre_family, field, COUNT(*) n FROM elicitation"
      " GROUP BY genre_family, field"
    )
  }


def next_question(
  conn: sqlite3.Connection,
  *,
  rng: random.Random | None = None,
  target: int = TARGET_PER_CELL,
) -> Question | None:
  """Pick the next comparison to ask, serving the emptiest cell first.

  Args:
    conn: Open connection.
    rng: Source of shuffling, injectable for tests.
    target: Answers wanted per cell before it is considered done.

  Returns:
    The next question, or None when every reachable cell is full.
  """
  rng = rng or random.Random()
  counts = cell_counts(conn)
  placeholders = ",".join("?" * len(ELICITABLE))
  rows = conn.execute(
    "SELECT t.id, t.genre_family, c.field,"
    # an unresolved track has no resolved_field, and a question with no
    # context does not say which track you are judging.
    " COALESCE((SELECT value FROM resolved_field"
    "           WHERE track_id=t.id AND field='title'), t.norm_title) title,"
    " COALESCE((SELECT value FROM resolved_field"
    "           WHERE track_id=t.id AND field='artist'), t.norm_artist) artist"
    " FROM track t JOIN field_candidate c ON c.track_id = t.id"
    f" WHERE c.value != '' AND c.field IN ({placeholders})"
    "   AND NOT EXISTS (SELECT 1 FROM elicitation e"
    "                   WHERE e.track_id = t.id AND e.field = c.field)"
    " GROUP BY t.id, c.field"
    " HAVING COUNT(DISTINCT c.source) >= 2",
    ELICITABLE,
  ).fetchall()

  # neediest cell first, then random within it, so the session covers every
  # family evenly instead of marching down the playlist it was ingested from.
  candidates_by_cell: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
  for row in rows:
    cell = (row["genre_family"] or "other", row["field"])
    if counts.get(cell, 0) < target:
      candidates_by_cell[cell].append(row)
  if not candidates_by_cell:
    return None

  for cell in sorted(candidates_by_cell, key=lambda c: (counts.get(c, 0), c)):
    pool = candidates_by_cell[cell]
    rng.shuffle(pool)
    for row in pool:
      question = _question_for(conn, row, rng)
      # a row can still collapse to one option: two sources agreeing on the
      # value differ only by source, which is not a question.
      if question is not None:
        return question
  return None


def _question_for(
  conn: sqlite3.Connection, row: sqlite3.Row, rng: random.Random
) -> Question | None:
  """Load a track's candidates and build the question for one field."""
  candidates = [
    FieldCandidate(
      field=r["field"],
      value=r["value"],
      source=r["source"],
      confidence=r["confidence"] or 1.0,
    )
    for r in conn.execute(
      "SELECT field, value, source, confidence FROM field_candidate WHERE track_id=?",
      (row["id"],),
    )
  ]
  return build_question(
    row["id"],
    row["genre_family"] or "other",
    row["field"],
    candidates,
    rng=rng,
    title=row["title"] or "",
    artist=row["artist"] or "",
  )


def apply(conn: sqlite3.Connection, *, min_observations: int = MIN_OBSERVATIONS) -> int:
  """Write the derived rankings into the `precedence` table.

  Only calibrated cells are written; everything else keeps the built-in
  default, which `load_precedence` falls back to.

  Args:
    conn: Open connection.
    min_observations: Answers required before a cell is written.

  Returns:
    The number of cells written.
  """
  derived = derive(observations(conn), min_observations=min_observations)
  for (family, field_name), ranked in derived.items():
    conn.execute(
      "DELETE FROM precedence WHERE genre_family = ? AND field = ?",
      (family, field_name),
    )
    for rank, source in enumerate(ranked):
      conn.execute(
        "INSERT INTO precedence (genre_family, field, rank, source) VALUES (?,?,?,?)",
        (family, field_name, rank, source),
      )
  return len(derived)


@dataclass(frozen=True)
class Progress:
  """How far through the session the user is."""

  answered: int
  cells_done: int
  cells_started: int
  remaining: int


def progress(conn: sqlite3.Connection, *, target: int = TARGET_PER_CELL) -> Progress:
  """Summarise the session.

  `remaining` counts questions still askable rather than a nominal total: a
  cell whose tracks are exhausted can never reach its target, and counting it
  as outstanding would leave the progress bar stalled short of the end forever.
  It is an upper bound — a pair can still collapse to a single option when the
  sources turn out to agree, and is then skipped without being asked.

  Args:
    conn: Open connection.
    target: Answers wanted per cell.

  Returns:
    A Progress summary.
  """
  counts = cell_counts(conn)
  placeholders = ",".join("?" * len(ELICITABLE))
  available = {
    (row["family"] or "other", row["field"]): row["n"]
    for row in conn.execute(
      "SELECT family, field, COUNT(*) n FROM ("
      "  SELECT t.id AS id, t.genre_family AS family, c.field AS field"
      "  FROM track t JOIN field_candidate c ON c.track_id = t.id"
      f"  WHERE c.value != '' AND c.field IN ({placeholders})"
      "    AND NOT EXISTS (SELECT 1 FROM elicitation e"
      "                    WHERE e.track_id = t.id AND e.field = c.field)"
      "  GROUP BY t.id, c.field HAVING COUNT(DISTINCT c.source) >= 2"
      ") GROUP BY family, field",
      ELICITABLE,
    )
  }
  remaining = sum(
    min(max(target - counts.get(cell, 0), 0), pairs)
    for cell, pairs in available.items()
  )
  return Progress(
    answered=sum(counts.values()),
    cells_done=sum(1 for n in counts.values() if n >= target),
    cells_started=len(counts),
    remaining=remaining,
  )
