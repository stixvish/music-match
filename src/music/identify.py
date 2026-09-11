"""Identity confidence (SPEC.md §12).

Confidence gates **entry, not ranking**. One threshold, one ordered precedence
list. If we are not confident which recording this is, none of its candidates
are used and the track goes to review — it is never published on a guess.
"""

import difflib
import re
from dataclasses import dataclass
from enum import StrEnum

# below this, nothing is written and the track goes to the review queue.
AUTO_ACCEPT = 0.80

# a matched title that adds one of these when the query did not ask for it is
# very likely the wrong version. measured need: a text search returned
# "SMASH! (instrumental)" at full confidence with a matching duration.
VARIANT_WORDS = (
  "instrumental",
  "sped up",
  "slowed",
  "reverb",
  "live",
  "radio edit",
  "acoustic",
  "karaoke",
  "a cappella",
  "acapella",
  "demo",
  "clean",
)

DURATION_TOLERANCE_S = 3.0


class Evidence(StrEnum):
  """How an identity was arrived at."""

  URL_OVERRIDE = "url_override"
  ISRC = "isrc"
  ACOUSTID = "acoustid"
  TEXT = "text"
  NONE = "none"


def distinct_rival_gap(
  candidates: list[tuple[str, float | None, int]],
  tolerance_s: float = DURATION_TOLERANCE_S,
) -> int:
  """Score gap between the top candidate and the best *genuinely different* one.

  A search for one song routinely returns several results all scoring 100 —
  the same recording catalogued on an album, a single and a compilation. That
  is not ambiguity about which song this is, and treating it as such rejects
  perfect matches. Only a candidate with a different title, or a materially
  different length, counts as a rival.

  Args:
    candidates: (title, length_seconds, score) ordered best-first.
    tolerance_s: Length difference that makes a candidate genuinely different.

  Returns:
    The score gap, or a large number when there is no distinct rival.
  """
  if not candidates:
    return 99
  top_title, top_length, top_score = candidates[0]
  for title, length, score in candidates[1:]:
    same_title = normalised_equal(title, top_title)
    same_length = (
      top_length is not None
      and length is not None
      and abs(length - top_length) <= tolerance_s
    )
    if same_title and same_length:
      continue  # same recording, different release
    return top_score - score
  return 99


@dataclass(frozen=True)
class Match:
  """Everything the scorer needs. Pure data — no i/o, no network."""

  evidence: Evidence
  search_score: int | None = None
  duration_delta_s: float | None = None
  rival_gap: int | None = None
  acoustid_score: float | None = None
  variant_mismatch: bool = False
  artist_unrelated: bool = False

  @property
  def duration_ok(self) -> bool:
    """Whether the candidate's length matches ours closely enough."""
    return (
      self.duration_delta_s is not None
      and abs(self.duration_delta_s) <= DURATION_TOLERANCE_S
    )


def variant_mismatch(query_title: str, matched_title: str) -> bool:
  """Whether the match adds a version qualifier the query did not ask for.

  Args:
    query_title: What we searched for.
    matched_title: What the source returned.

  Returns:
    True if the match introduces an unrequested variant.
  """
  query = (query_title or "").casefold()
  matched = (matched_title or "").casefold()
  return any(w in matched and w not in query for w in VARIANT_WORDS)


_WS = re.compile(r"\s+")


# Below this, a result is not a plausible rendering of what we searched for.
# Measured against the first hundred-track run: the catastrophic mismatches —
# a different song by a different artist — scored 0.14 to 0.36, while every
# correct match scored 0.58 or above, including those where the query artist
# was a channel name (`jayseanworldwide` -> `Jay Sean`, 0.88).
#
# A *cover* is not caught here and is not meant to be: it carries the right
# title, so `Luke Conard - We Are Never Ever Getting Back Together` scores
# 0.75 against a Taylor Swift query. Ranking handles that case — the original
# scores higher and wins — and this gate only stops a result being used when
# nothing fetched resembles the query at all.
PLAUSIBLE = 0.45

# How much an unrequested version designation costs when ranking results.
# Enough that `Marvin Gaye (Remix)` loses to `Marvin Gaye (feat. Meghan
# Trainor)` for a plain `Marvin Gaye` query, and that the original beats a
# re-recording, while a correct match carrying one still clears PLAUSIBLE.
VERSION_PENALTY = 0.25


def _fold(text: str) -> str:
  return _WS.sub(" ", re.sub(r"[^\w\s]", " ", (text or "").casefold())).strip()


def match_score(query_artist: str, query_title: str, artist: str, title: str) -> float:
  """Score how well a source's result renders what we searched for.

  One function for every source, so "which of these five is the right one" is
  answered the same way everywhere. Before this existed, iTunes and Spotify
  fetched five results and kept `[0]`, which is frequently a remaster, a live
  cut or a re-recording with the original further down the list.

  Title dominates and artist supports rather than gates: the query artist is
  often a YouTube channel name (`jayseanworldwide`, `push baby`), so a weak
  artist match must not veto a perfect title. Within one source's results the
  query is constant, so a low artist weight still ranks them correctly.

  Args:
    query_artist: Normalised artist we searched for.
    query_title: Normalised title we searched for.
    artist: Artist the source returned.
    title: Title the source returned.

  Returns:
    A score from 0.0 to 1.0.
  """
  from music.publish.naming import strip_version

  def ratio(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, _fold(left), _fold(right)).ratio()

  query_core, title_core = strip_version(query_title), strip_version(title)
  # an exact rendering counts for more than a matching core: "Love Story" and
  # "Love Story (Taylor's Version)" share a core and are different recordings
  title_sim = 0.7 * ratio(query_title, title) + 0.3 * ratio(query_core, title_core)
  score = 0.7 * title_sim + 0.3 * ratio(query_artist, artist)

  # A version designation the query never asked for means a different
  # recording. This deliberately uses `strip_version`'s vocabulary rather than
  # VARIANT_WORDS: that list feeds `confidence`, and adding "remix" to it would
  # drop every legitimate remix in an electronic library below auto-accept.
  # A `feat.` clause is not a version and `strip_version` leaves it alone.
  if title_core != title and query_core == query_title:
    score -= VERSION_PENALTY
  if variant_mismatch(query_title, title):
    score -= 0.12
  return max(0.0, min(1.0, score))


def is_plausible(query_artist: str, query_title: str, artist: str, title: str) -> bool:
  """Whether a result is close enough to be the thing we asked for.

  Args:
    query_artist: Normalised artist we searched for.
    query_title: Normalised title we searched for.
    artist: Artist the source returned.
    title: Title the source returned.

  Returns:
    True if the result is a believable match.
  """
  return match_score(query_artist, query_title, artist, title) >= PLAUSIBLE


def artist_is_unrelated(query_artist: str, matched_artist: str) -> bool:
  """Whether a match's artist bears no relation to the one we searched for.

  This is the cover signature. A fingerprint match carries the right title and
  a stranger's name: `Waiting for Love` by "Die NotenDealer", `We Are Never
  Ever Getting Back Together` by "Luke Conard". Title similarity cannot
  distinguish those from the real thing, so the artist has to be checked
  separately — but only as a confidence signal, never as a selection filter.

  A YouTube channel name usually *contains* the artist (`jayseanworldwide`,
  `iyazlive`, `seankingston`), so containment either way counts as related.
  A renamed band does not (`push baby` is Rixton), which this will flag for
  review — the wrong way round is publishing a cover as the original.

  Args:
    query_artist: Normalised artist we searched for.
    matched_artist: Artist the match is credited to.

  Returns:
    True when the two share nothing.
  """
  left = re.sub(r"[^a-z0-9]", "", (query_artist or "").casefold())
  right = re.sub(r"[^a-z0-9]", "", (matched_artist or "").casefold())
  if not left or not right:
    return False
  if left in right or right in left:
    return False
  return difflib.SequenceMatcher(None, left, right).ratio() < 0.5


def confidence(match: Match) -> float:
  """Score an identity from 0.0 to 1.0 (SPEC.md §12).

  Args:
    match: The evidence.

  Returns:
    Confidence. Compare against `AUTO_ACCEPT`.
  """
  if match.evidence is Evidence.NONE:
    return 0.0

  base: float
  if match.evidence is Evidence.URL_OVERRIDE:
    base = 1.00
  elif match.evidence is Evidence.ISRC:
    base = 0.98
  elif match.evidence is Evidence.ACOUSTID:
    strong = (match.acoustid_score or 0.0) >= 0.90 and match.duration_ok
    base = 0.95 if strong else 0.50
    # a fingerprint match credited to a stranger is a cover, and covers are
    # the one wrong answer that title scoring cannot see (SPEC.md §12)
    if match.artist_unrelated:
      base = min(base, 0.50)
  else:
    score = match.search_score or 0
    # a close rival means the search could not tell two recordings apart
    contested = match.rival_gap is not None and match.rival_gap < 2
    base = 0.85 if (score >= 90 and match.duration_ok and not contested) else 0.50

  # an unrequested version qualifier drops the match below auto-accept, because
  # being confidently wrong about the version is worse than asking.
  if match.variant_mismatch:
    base = min(base, 0.50)
  return round(base, 2)


def should_auto_accept(match: Match) -> bool:
  """Whether this identity may be written without human review.

  Args:
    match: The evidence.

  Returns:
    True if confidence reaches the threshold.
  """
  return confidence(match) >= AUTO_ACCEPT


def review_reason(match: Match) -> str | None:
  """Why a track needs review, for the queue.

  Args:
    match: The evidence.

  Returns:
    A `review_queue.reason` value, or None if no review is needed.
  """
  if should_auto_accept(match):
    return None
  return "no_match" if match.evidence is Evidence.NONE else "low_confidence"


def normalised_equal(left: str, right: str) -> bool:
  """Loose string equality for comparing a query against a match.

  Args:
    left: First string.
    right: Second string.

  Returns:
    True if they differ only by case, punctuation or spacing.
  """

  def strip(text: str) -> str:
    return _WS.sub(" ", re.sub(r"[^\w\s]", "", (text or "").casefold())).strip()

  return strip(left) == strip(right)
