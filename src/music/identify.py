"""Identity confidence (SPEC.md §12).

Confidence gates **entry, not ranking**. One threshold, one ordered precedence
list. If we are not confident which recording this is, none of its candidates
are used and the track goes to review — it is never published on a guess.
"""

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


_WS = re.compile(r"\s+")


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
