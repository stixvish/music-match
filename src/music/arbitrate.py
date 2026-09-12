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

import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from music.publish.naming import split_artists, strip_version
from music.sources.base import ALBUM_GROUP, FieldCandidate

# Fields where agreement between sources is evidence rather than mere
# popularity, because exactly one value is correct. `genre` is excluded: sources
# disagree in granularity by design, and two coarse sources both saying "Dance"
# must not outrank Discogs' "Progressive House" — that ranking is what
# elicitation calibrates (§9). `artwork_url` is excluded because several covers
# are all genuinely correct.
JUDGEMENT_FIELDS = ("genre", "artwork_url")

# Fields where sources differ by *convention* rather than by fact, per family.
# Two catalogues following the same convention and agreeing is not independent
# evidence — it is the same convention counted twice — so consensus is skipped
# and precedence decides.
#
# Indian film music credits the music director as the track artist: for
# "Sanam Re" iTunes returns `Mithoon & Arijit Singh` and Spotify returns
# `Mithoon`, both naming the composer, while MusicBrainz returns the vocalist
# `Arijit Singh`. The house convention is vocalists on the artist line and the
# fuller credit on album artist, so `artist` is decided by precedence in this
# family and `album_artist` is deliberately left alone.
CONVENTION_FIELDS: dict[str, tuple[str, ...]] = {"world": ("artist",)}


def _skips_consensus(family: str, field: str) -> bool:
  """Whether agreement between sources is evidence for this field.

  Args:
    family: Genre family.
    field: Field name.

  Returns:
    True when precedence should decide instead of agreement.
  """
  return field in JUDGEMENT_FIELDS or field in CONVENTION_FIELDS.get(family, ())


# dates are not chosen by precedence. SPEC.md §7 wants the *earliest* release
# of the recording, and sources disagree in both value and precision: for
# "Tum Hi Ho" musicbrainz returned a bare "2013" while itunes returned
# "2013-03-16". Taking the first-ranked source loses the precise date.
DATE_FIELDS = ("release_date", "year")

# album credits that mean "this is a compilation", not an album by the artist
COMPILATION_CREDITS = frozenset(
  {"various artists", "various", "va", "verschiedene interpreten", "diverse"}
)

# discogs writes this for self-released and white-label pressings. it is
# accurate and useless; it is not a label name.
NON_LABELS = frozenset({"not on label", "self-released", "none", "unknown"})

# edition markers, matched against an album title (SPEC.md §7)
DELUXE_WORDS = (
  "deluxe",
  "expanded",
  "special",
  "anniversary",
  "complete",
  "extended",
  # David Guetta ships "Nothing But the Beat", "Nothing but the Beat 2.0" and
  # "Nothing But the Beat Ultimate"; the last is the fullest.
  "ultimate",
)
_EDITION_SUFFIX = re.compile(
  r"\s*[\(\[][^)\]]*\b(?:"
  + "|".join(DELUXE_WORDS)
  + r"|version|edition)\b[^)\]]*[\)\]]\s*$",
  re.IGNORECASE,
)
# The same markers written without brackets. Without this, "Nothing But the
# Beat", "Nothing but the Beat 2.0" and "Nothing But the Beat Ultimate" are
# three unrelated albums, one per source, so no group ever agrees and the
# edition each track lands on is decided by whichever source happens to rank
# first — which is why one Guetta track got 2.0 and another got Ultimate.
_EDITION_TAIL = re.compile(
  r"\s+(?:" + "|".join(DELUXE_WORDS) + r"|remastered|reissue)"
  r"(?:\s+(?:edition|version))?\s*$",
  re.IGNORECASE,
)
# A decimal version suffix only: a bare trailing integer is part of the title
# far more often than it is an edition ("Kidz Bop 22", "Blink-182").
_VERSION_TAIL = re.compile(r"\s+\d+\.\d+\s*$")


def _album_key(title: str) -> str:
  """Group editions of one album together.

  "Planet Pit" and "Planet Pit (Deluxe Version)" are the same release.

  Args:
    title: An album title.

  Returns:
    A normalised grouping key.
  """
  base = _EDITION_SUFFIX.sub("", title or "")
  # strip repeatedly: "Nothing But the Beat 2.0 (Deluxe)" carries both forms
  for pattern in (_VERSION_TAIL, _EDITION_TAIL):
    while True:
      stripped = pattern.sub("", base)
      if stripped == base:
        break
      base = stripped
  return "".join(ch for ch in base.casefold() if ch.isalnum())


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
  # quality order, consulted only among sources that named the same release;
  # release-accuracy is enforced first, in `_artwork_for`.
  "artwork_url": ("itunes", "spotify", "musicbrainz"),
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
    # musicbrainz first for the performer: itunes and spotify both credit the
    # music director, which is the film-industry convention, not the singer
    "artist": ("musicbrainz", "itunes", "spotify"),
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


def _norm(text: str) -> str:
  return "".join(ch for ch in (text or "").casefold() if ch.isalnum())


def _is_compilation(
  candidates: Sequence[FieldCandidate],
  source: str,
  artist: str = "",
  *,
  credit_differs_by_convention: bool = False,
) -> bool:
  """Whether a source's album looks like a compilation rather than the album.

  Two signals, the second much more general than the first:

  1. an explicit various-artists credit
  2. **an album artist that is not the track artist.** Only when the two are
     expected to match: Indian film music credits the music director on the
     album and the singer on the track, so this signal is off for that family
     or every Bollywood album is discarded as a compilation. "Fireball"
     resolved to
     "Mastermix Classic Cuts, Volume 165" credited to "Music Factory" — a DJ
     service compilation that never says "Various Artists". If the track is by
     Pitbull, the album it belongs to is credited to Pitbull.

  Args:
    candidates: All candidates for a track.
    source: Source name to check.
    artist: The track's artist, for signal 2.
    credit_differs_by_convention: Disables signal 2 for families where the
      album and track credits are expected to differ.

  Returns:
    True if that source's album looks like a compilation.
  """
  credit = next(
    (c.value for c in candidates if c.source == source and c.field == "album_artist"),
    "",
  )
  if not credit:
    return False
  if credit.strip().casefold() in COMPILATION_CREDITS:
    return True
  if not artist or credit_differs_by_convention:
    # signal 2 only works where the album and the track are credited to the
    # same act. In Indian film music they are not, so an explicit
    # various-artists credit is the only signal left.
    return False
  left, right = _norm(credit), _norm(artist)
  # a substring match covers "David Guetta" vs "David Guetta & Akon"
  return not (left == right or left.startswith(right) or right.startswith(left))


def _best_album_source(
  candidates: Sequence[FieldCandidate],
  ranked: Iterable[str],
  artist: str = "",
  *,
  loose_credit: bool = False,
) -> str | None:
  """Pick one source to supply every album field.

  A source whose album credit is "Various Artists" is skipped while any other
  source offers album fields. Measured need: MusicBrainz won on precedence and
  returned "NRJ Hits 2011" and "Dog Days of Summer ... Sampler" while Spotify
  and iTunes had the actual albums. A compilation is a place the track appears,
  not the album it belongs to.

  Args:
    candidates: All candidates for a track.
    ranked: Sources in precedence order.
    artist: The *arbitrated* artist. Compilation detection compares each
      source's album credit against it, so passing an arbitrary candidate
      inverts the test: when a bad source won `artist`, every correct album
      looked like a compilation and was discarded.
    loose_credit: Set when the family credits the album and the track
      differently by convention, which disables that comparison.

  Returns:
    The winning source name, or None if no source offers any album field.
  """
  offered: dict[str, set[str]] = {}
  for candidate in candidates:
    if candidate.field in ALBUM_GROUP and candidate.value:
      offered.setdefault(candidate.source, set()).add(candidate.field)
  if not offered:
    return None

  real = [
    s
    for s in offered
    if not _is_compilation(
      candidates, s, artist, credit_differs_by_convention=loose_credit
    )
  ]
  if not real:
    # every source offered only a compilation. "Down" resolved to
    # "Ultra Dance 11" credited to DJ Enferno, which is where the track was
    # licensed, not the album it belongs to. No album is honest; a wrong one
    # is not, and it propagates into the filename and both DJ apps.
    return None
  pool = real
  albums = {
    c.source: c.value
    for c in candidates
    if c.field == "album" and c.value and c.source in pool
  }
  if not albums:
    return next((s for s in ranked if s in pool), pool[0])

  # group editions of one album together: "Planet Pit" and "Planet Pit
  # (Deluxe Version)" are one release, not two competing candidates.
  groups: dict[str, list[str]] = {}
  for source, album in albums.items():
    groups.setdefault(_album_key(album), []).append(source)

  order = list(ranked)

  def group_rank(item: tuple[str, list[str]]) -> tuple[int, int]:
    _, sources = item
    # agreement first: two sources naming the same album outweigh one ranked
    # higher. "Hey Baby" took musicbrainz's "Global Warming" while itunes and
    # spotify both said "Planet Pit (Deluxe Version)" — and were right.
    best = min((order.index(s) for s in sources if s in order), default=len(order))
    return (-len(sources), best)

  winning = min(groups.items(), key=group_rank)[1]

  # within the winning album, prefer the largest edition (SPEC.md §7)
  def edition_rank(source: str) -> tuple[int, int, int]:
    title = albums[source].casefold()
    deluxe = any(w in title for w in DELUXE_WORDS)
    # a numbered reissue ("2.0") is fuller than the plain album but not as
    # full as a named deluxe edition
    numbered = bool(_VERSION_TAIL.search(albums[source]))
    return (
      -int(deluxe),
      -int(numbered),
      order.index(source) if source in order else len(order),
    )

  return min(winning, key=edition_rank)


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

  # the artist is decided first: the album group's compilation test is
  # relative to it, so it has to be the arbitrated value rather than whichever
  # candidate happens to come first.
  artist_options = by_field.get("artist", [])
  artist_winner = (
    _consensus(artist_options, ranking(family, "artist"), "artist")
    if artist_options and not _skips_consensus(family, "artist")
    else next(
      (
        c
        for source in ranking(family, "artist")
        for c in artist_options
        if c.source == source
      ),
      artist_options[0] if artist_options else None,
    )
  )
  artist_value = artist_winner.value if artist_winner else ""

  # album fields come from a single source, chosen once (SPEC.md §7)
  album_source = _best_album_source(
    candidates,
    ranking(family, "album"),
    artist_value,
    loose_credit=bool(CONVENTION_FIELDS.get(family)),
  )
  album_value = ""
  for field in ALBUM_GROUP:
    if album_source is None:
      break
    match = next((c for c in by_field.get(field, []) if c.source == album_source), None)
    if match:
      decisions.append(Decision(field, match.value, match.source))
      if field == "album":
        album_value = match.value

  # the cover belongs to the release we just tagged, so it is chosen against
  # that album rather than by precedence of its own.
  art = _artwork_for(
    by_field.get("artwork_url", []),
    candidates,
    album_source,
    album_value,
    ranking(family, "artwork_url"),
  )
  if art is not None:
    decisions.append(Decision("artwork_url", art.value, art.source))

  for field, options in sorted(by_field.items()):
    if field in ALBUM_GROUP or field == "artwork_url":
      continue
    if field == "label":
      options = [c for c in options if not _is_non_label(c.value)]
      if not options:
        continue
    if field in DATE_FIELDS:
      chosen = _earliest(options)
      decisions.append(Decision(field, chosen.value, chosen.source))
      continue
    ranked = ranking(family, field)
    winner = (
      None if _skips_consensus(family, field) else _consensus(options, ranked, field)
    )
    if winner is None:
      winner = next(
        (c for source in ranked for c in options if c.source == source), None
      )
    if winner is not None:
      decisions.append(Decision(field, winner.value, winner.source))
      continue
    # no ranked source had a value. take an unranked one rather than lose the
    # field, but mark it so it can be audited — this is how itunes' coarse
    # "Dance" won genre on an electronic track.
    spare = options[0]
    decisions.append(Decision(field, spare.value, spare.source, decided_by="fallback"))

  return sorted(decisions, key=lambda d: d.field)


def _consensus(
  options: Sequence[FieldCandidate], ranked: Sequence[str], field: str = ""
) -> FieldCandidate | None:
  """Choose a value, letting sources that agree outweigh one ranked higher.

  This is the album rule (`_best_album_source`) applied to every other factual
  field, which is where it was missing and where it cost the most. Measured on
  the first hundred-track run: for "Last Night", iTunes and Spotify both
  returned `Morgan Wallen - Last Night` and MusicBrainz returned
  `Metro Station - California`; MusicBrainz ranks first for pop, so it won both
  `artist` and `title` and the track was published as the wrong song. Twelve of
  eighty-three published tracks failed this way.

  Agreement is counted in distinct sources, and ties fall back to precedence,
  so a field only one source offers behaves exactly as before.

  Args:
    options: Candidates for one field, all non-empty.
    ranked: Sources in precedence order.
    field: Field name, which decides how values are loosened when nothing
      agrees exactly.

  Returns:
    The winning candidate, or None when there is nothing to choose between.
  """
  if not options:
    return None
  order = list(ranked)

  def group_by(key: Callable[[str], str]) -> list[list[FieldCandidate]]:
    groups: dict[str, list[FieldCandidate]] = {}
    for candidate in options:
      groups.setdefault(key(candidate.value), []).append(candidate)
    return list(groups.values())

  def group_rank(group: list[FieldCandidate]) -> tuple[int, int]:
    sources = {c.source for c in group}
    best = min((order.index(s) for s in sources if s in order), default=len(order))
    return (-len(sources), best)

  def distinct(group: list[FieldCandidate]) -> int:
    return len({c.source for c in group})

  winning = min(group_by(_norm), key=group_rank)
  if distinct(winning) < 2:
    # No two sources agree exactly — but they may agree on the song and differ
    # only on a version qualifier, which splits them and hands the field to an
    # unrelated third source. "Hey, Soul Sister" was published as "There for
    # You" because iTunes said `Hey, Soul Sister (Country Mix)`, Spotify said
    # `Hey, Soul Sister`, and MusicBrainz — describing a different recording
    # entirely — outranked both. A `feat.` credit is never stripped: it names
    # the same recording more completely, not a different one.
    looser = min(group_by(lambda v: _norm(strip_version(v))), key=group_rank)
    if distinct(looser) >= 2:
      winning = looser
    else:
      # Still nothing. One source may simply spell the credit more fully:
      # iTunes returned `Run This Town (feat. Rihanna & Kanye West)` and
      # `JAY-Z & Kanye West` where Spotify returned `Run This Town` and
      # `JAY-Z`. They describe the same recording; MusicBrainz, describing
      # Bonnie Tyler, agreed with neither and won on precedence alone. A value
      # that shares its core with nobody should not beat two that share one.
      core = min(group_by(lambda v: _core_key(field, v)), key=group_rank)
      if distinct(core) >= 2:
        # within a core, the fullest credit wins, provided it is an extension
        # of the others rather than a different string
        return max(
          core,
          key=lambda c: (
            len(c.value),
            -(order.index(c.source) if c.source in order else len(order)),
          ),
        )
  # within the winning value, still prefer the highest-ranked source, so
  # provenance stays meaningful
  return min(
    winning,
    key=lambda c: order.index(c.source) if c.source in order else len(order),
  )


def _core_key(field: str, value: str) -> str:
  """Reduce a value to the part that identifies the work, not the credit.

  For an artist that is the primary name alone ("JAY-Z & Kanye West" and
  "JAY-Z" share a core); for anything else it is the value with every
  parenthesised or bracketed suffix removed.

  Args:
    field: Field the value belongs to.
    value: The candidate value.

  Returns:
    A normalised comparison key.
  """
  if field == "artist":
    parts = split_artists(value)
    return _norm(parts[0]) if parts else _norm(value)
  return _norm(re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", value))


def _artwork_for(
  options: Sequence[FieldCandidate],
  candidates: Sequence[FieldCandidate],
  album_source: str | None,
  album: str,
  ranked: Sequence[str],
) -> FieldCandidate | None:
  """Choose the cover that belongs to the album we tagged.

  Artwork used to be resolved by its own precedence, independently of the
  album group. MusicBrainz carries no cover art, so whenever it won the album —
  which is most of the time for pop — the cover fell to iTunes, showing the
  sleeve of whichever release *iTunes* had matched. Every field agreed and the
  picture was from another record: `Body & Soul` tagged to "The Juicebox" with
  the cover of "The Juice, Vol. II", `Outta My Head` tagged to "Free Spirit"
  with a cover by an unrelated artist.

  A cover is not a matter of taste, which is how it came to be exempted from
  agreement. It is the cover *of one release*, so it is chosen by release:

  1. the source that supplied the album, if it offers artwork at all
  2. any source whose own album is the same release (editions included)
  3. precedence, as a last resort, so a track never loses its art entirely

  Args:
    options: Artwork candidates.
    candidates: Every candidate, for reading each source's album.
    album_source: The source the album group came from, if any.
    album: The album title that was chosen.
    ranked: Artwork precedence, used only at step 3.

  Returns:
    The winning artwork candidate, or None when no source offers one.
  """
  usable = [c for c in options if c.value]
  if not usable:
    return None
  order = list(ranked)

  def by_precedence(pool: Sequence[FieldCandidate]) -> FieldCandidate:
    return min(
      pool, key=lambda c: order.index(c.source) if c.source in order else len(order)
    )

  # Every source that named the same release is equally correct, so among those
  # the ranking is free to be about quality. Measured 2026-09-11: iTunes serves
  # 1200x1200 at 294-546 KB and Spotify 640x640 at 126-180 KB — roughly four
  # times the pixel area — so iTunes leads, and the Cover Art Archive sits last
  # despite being the most release-accurate, because its scans are
  # user-contributed and vary.
  if album:
    wanted = _album_key(album)
    albums = {c.source: c.value for c in candidates if c.field == "album" and c.value}
    same_release = [c for c in usable if _album_key(albums.get(c.source, "")) == wanted]
    if same_release:
      return by_precedence(same_release)

  # Nobody else has the release we tagged. The album source's own cover is then
  # the only one certainly of the right record, and correct-but-smaller beats
  # larger-and-wrong — which is the whole reason this function exists.
  if album_source:
    owned = next((c for c in usable if c.source == album_source), None)
    if owned is not None:
      return owned

  return by_precedence(usable)


def _is_non_label(value: str) -> bool:
  """Whether a label value is a placeholder rather than a label.

  Discogs writes "Not On Label (Pitbull)" for self-released pressings.

  Args:
    value: A candidate label.

  Returns:
    True if the value is a placeholder.
  """
  text = value.strip().casefold()
  base = text.split("(")[0].strip()
  return base in NON_LABELS or not base


def _earliest(options: Sequence[FieldCandidate]) -> FieldCandidate:
  """Pick the earliest date, preferring precision when the year is the same.

  Args:
    options: Date candidates from different sources.

  Returns:
    The winning candidate.
  """

  def key(candidate: FieldCandidate) -> tuple[str, int, str]:
    value = candidate.value.strip()
    # earlier year first; within a year, more precision wins; between two
    # equally precise dates, the earlier one wins. comparing the full string
    # first would make a bare "2013" beat "2013-03-16", losing precision.
    return (value[:4], -len(value), value)

  return min(options, key=key)


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


def rearbitrate(conn: sqlite3.Connection, track_id: int) -> int:
  """Re-run arbitration for one track from its stored candidates.

  Nothing is re-downloaded and no source is re-queried: `field_candidate` is
  append-only and already holds every value every source offered, so a
  resolver improvement can be applied to a whole library offline (SPEC.md §11).

  Fields the user decided by hand are left alone, because `persist` refuses to
  overwrite them.

  Args:
    conn: Open connection.
    track_id: Track to re-decide.

  Returns:
    How many fields ended up with a different value.
  """
  row = conn.execute(
    "SELECT genre_family FROM track WHERE id = ?", (track_id,)
  ).fetchone()
  if row is None:
    return 0
  candidates = [
    FieldCandidate(
      field=r["field"],
      value=r["value"],
      source=r["source"],
      confidence=r["confidence"] or 1.0,
    )
    for r in conn.execute(
      "SELECT field, value, source, confidence FROM field_candidate WHERE track_id = ?",
      (track_id,),
    )
    if r["value"]
  ]
  if not candidates:
    return 0

  before = {
    r["field"]: r["value"]
    for r in conn.execute(
      "SELECT field, value, decided_by FROM resolved_field WHERE track_id = ?",
      (track_id,),
    )
    if r["decided_by"] != "manual"
  }
  decisions = arbitrate(candidates, family=row["genre_family"] or "other")
  # a field that no longer resolves must not keep its stale value: dropping a
  # compilation album means the album really is unknown now.
  keep = {d.field for d in decisions}
  conn.execute(
    "DELETE FROM resolved_field WHERE track_id = ? AND decided_by != 'manual'",
    (track_id,),
  )
  persist(conn, track_id, decisions)
  after = {d.field: d.value for d in decisions if d.field in keep}
  fields = set(before) | set(after)
  return sum(1 for f in fields if before.get(f) != after.get(f))
