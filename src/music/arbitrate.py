"""Decide which source wins each field (SPEC.md §7, §12).

Genre selects the table; the table ranks sources per field. First source
holding a candidate wins — **confidence gates entry, not ranking**, so a track
whose identity is uncertain contributes no candidates at all rather than being
ranked lower.

Two rules stop individually-defensible answers from being collectively wrong:

- **Album fields resolve as a group** from one source. Taking the album name
  from a deluxe edition and the track number from the standard yields track 14
  of a 12-track album.
- **Manual edits are never overwritten.** Re-running the resolver must always
  be safe (SPEC.md §15).
"""

import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from music.sources.base import ALBUM_GROUP, FieldCandidate

# dates are not chosen by precedence. SPEC.md §7 wants the *earliest* release
# of the recording, and sources disagree in both value and precision: for
# "Tum Hi Ho" musicbrainz returned a bare "2013" while itunes returned
# "2013-03-16". Taking the first-ranked source loses the precise date.
DATE_FIELDS = ("release_date", "year")

# default ranking per field, before elicitation calibrates it (SPEC.md §9).
_BASE: dict[str, tuple[str, ...]] = {
  "title": ("musicbrainz", "spotify", "itunes", "discogs"),
  "artist": ("musicbrainz", "spotify", "itunes", "discogs"),
  "album": ("musicbrainz", "spotify", "itunes"),
  "album_artist": ("musicbrainz", "spotify", "itunes"),
  "track_number": ("musicbrainz", "spotify", "itunes"),
  "disc_number": ("musicbrainz", "spotify", "itunes"),
  # the earliest release across singles and albums, not the album's date (§7)
  "release_date": ("musicbrainz", "spotify", "itunes"),
  "year": ("musicbrainz", "spotify", "itunes"),
  "genre": ("discogs", "musicbrainz", "itunes", "essentia"),
  "label": ("discogs", "musicbrainz"),
  "catalog_number": ("discogs",),
  "isrc": ("spotify", "musicbrainz"),
  "composer": ("musicbrainz",),
  "lyricist": ("musicbrainz",),
  "remixer": ("derived", "musicbrainz", "discogs"),
  "mix_name": ("derived", "musicbrainz"),
  "original_artist": ("musicbrainz",),
  "artwork_url": ("itunes", "spotify"),
  "bpm": ("local", "beatport"),
  "key": ("local", "beatport"),
}

# per-family departures from the base ranking (SPEC.md §7).
_OVERRIDES: dict[str, dict[str, tuple[str, ...]]] = {
  "electronic": {
    # beatport is often the only source that knows the version exists
    "title": ("beatport", "musicbrainz", "spotify", "discogs"),
    "mix_name": ("derived", "beatport", "musicbrainz"),
    "remixer": ("derived", "beatport", "musicbrainz", "discogs"),
    "genre": ("beatport", "discogs", "musicbrainz", "essentia"),
    "label": ("beatport", "discogs", "musicbrainz"),
    "catalog_number": ("beatport", "discogs"),
  },
  "world": {
    # itunes has the strongest catalogue for regional music, where
    # musicbrainz and discogs are both thin (§7)
    "title": ("itunes", "musicbrainz", "spotify", "discogs"),
    "artist": ("itunes", "musicbrainz", "spotify"),
    "album": ("itunes", "musicbrainz", "spotify"),
    "album_artist": ("itunes", "musicbrainz", "spotify"),
    "track_number": ("itunes", "musicbrainz", "spotify"),
    "disc_number": ("itunes", "musicbrainz", "spotify"),
    "genre": ("itunes", "discogs", "musicbrainz", "essentia"),
  },
}


@dataclass(frozen=True)
class Decision:
  """One resolved field and the source it came from."""

  field: str
  value: str
  source: str
  decided_by: str = "precedence"


def precedence_for(family: str, field: str) -> tuple[str, ...]:
  """Ranked sources for a field within a genre family.

  Args:
    family: One of the six families (SPEC.md §7).
    field: Field name.

  Returns:
    Source names, best first. Empty when no source is ranked for the field.
  """
  override = _OVERRIDES.get(family, {}).get(field)
  return override if override is not None else _BASE.get(field, ())


def load_precedence(
  conn: sqlite3.Connection, family: str, field: str
) -> tuple[str, ...]:
  """Ranked sources, preferring the calibrated table over the defaults.

  The `precedence` table is populated by the elicitation exercise (SPEC.md §9);
  until then the built-in defaults apply.

  Args:
    conn: Open connection.
    family: Genre family.
    field: Field name.

  Returns:
    Source names, best first.
  """
  rows = conn.execute(
    "SELECT source FROM precedence WHERE genre_family = ? AND field = ? ORDER BY rank",
    (family, field),
  ).fetchall()
  return tuple(r["source"] for r in rows) or precedence_for(family, field)


def _best_album_source(
  candidates: Sequence[FieldCandidate], ranked: Iterable[str]
) -> str | None:
  """Pick one source to supply every album field.

  Args:
    candidates: All candidates for a track.
    ranked: Sources in precedence order.

  Returns:
    The winning source name, or None if no source offers any album field.
  """
  offered: dict[str, set[str]] = {}
  for candidate in candidates:
    if candidate.field in ALBUM_GROUP and candidate.value:
      offered.setdefault(candidate.source, set()).add(candidate.field)
  for source in ranked:
    if source in offered:
      return source
  return next(iter(offered), None)


def arbitrate(
  candidates: Sequence[FieldCandidate],
  family: str = "other",
  *,
  ranking: Callable[[str, str], tuple[str, ...]] = precedence_for,
) -> list[Decision]:
  """Choose a winning value for every field.

  Args:
    candidates: Every value every source offered.
    family: Genre family, selecting the precedence table.
    ranking: Callable `(family, field) -> sources`, injectable for the
      calibrated table or for tests.

  Returns:
    One Decision per resolved field, sorted by field name.
  """
  by_field: dict[str, list[FieldCandidate]] = {}
  for candidate in candidates:
    if candidate.value:
      by_field.setdefault(candidate.field, []).append(candidate)

  decisions: list[Decision] = []

  # album fields come from a single source, chosen once (SPEC.md §7)
  album_source = _best_album_source(candidates, ranking(family, "album"))
  for field in ALBUM_GROUP:
    if album_source is None:
      break
    match = next((c for c in by_field.get(field, []) if c.source == album_source), None)
    if match:
      decisions.append(Decision(field, match.value, match.source))

  for field, options in sorted(by_field.items()):
    if field in ALBUM_GROUP:
      continue
    ranked = ranking(family, field)
    winner = next((c for source in ranked for c in options if c.source == source), None)
    # a field nobody ranks still gets a value rather than being dropped
    winner = winner or options[0]
    decisions.append(Decision(field, winner.value, winner.source))

  return sorted(decisions, key=lambda d: d.field)


def persist(
  conn: sqlite3.Connection, track_id: int, decisions: Sequence[Decision]
) -> int:
  """Write decisions, leaving manual edits untouched.

  Args:
    conn: Open connection.
    track_id: Track being resolved.
    decisions: Arbitration output.

  Returns:
    How many fields were written.
  """
  manual = {
    row["field"]
    for row in conn.execute(
      "SELECT field FROM resolved_field WHERE track_id = ? AND decided_by = 'manual'",
      (track_id,),
    )
  }
  written = 0
  for decision in decisions:
    if decision.field in manual:
      continue  # a human decided this; the resolver never overrules it (§15)
    conn.execute(
      "INSERT OR REPLACE INTO resolved_field"
      " (track_id, field, value, source, decided_by) VALUES (?,?,?,?,?)",
      (track_id, decision.field, decision.value, decision.source, decision.decided_by),
    )
    written += 1
  return written
