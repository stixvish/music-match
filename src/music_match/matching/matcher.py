"""Choosing a match for a track, and deciding how much to trust it.

Two decisions live here, and they are different problems.

**Which candidate**, within a source, is a scoring question — handled by
`score.py`, mostly on duration and string similarity.

**Which value to write**, across sources, is a voting question, and it is
the one per-candidate scoring provably cannot solve. Searching AC/DC's
"Thunderstruck" returns the correct album `The Razors Edge` and the
soundtrack `Iron Man 2` with *identical* title, artist and duration; there
is nothing in either candidate to separate them. What separates them is
that three sources say one and one source says the other. So fields are
filled by precedence, but a value contradicted by the weight of other
sources loses to the value they agree on.

Confidence then reflects how much of that agreement there was, which is
what decides whether a match is applied automatically or queued for
review.
"""

import collections
import dataclasses
from typing import Iterable, Mapping, Protocol, Sequence

from music_match.config.loader import PrecedenceConfig
from music_match.matching import normalize as norm
from music_match.matching import score as scoring
from music_match.sources.base import SourceError
from music_match.sources.base import SourceQuery
from music_match.sources.base import SourceResult
from music_match.tagging.fields import ALL_FIELDS
from music_match.tagging.fields import TrackTags

# How many candidates to ask each source for. Taking the first result is
# right about half the time; scoring three and keeping the best is what
# this costs and buys.
CANDIDATES_PER_SOURCE = 3

# A candidate scoring below this is not the track, whatever its source
# ranked it. Tuned against tracks with known-correct answers.
MINIMUM_CANDIDATE_SCORE = 0.55

# Confidence at or above which a match is trusted without review. Tuned
# against tracks with known-correct answers: every correct match scored
# 0.90 or better, while the one genuinely ambiguous case — a radio edit
# that four sources placed on four different releases — scored 0.81. The
# gap is real but the sample is small, so this is a starting point to
# re-check as more of the library goes through, not a settled number.
DEFAULT_AUTO_APPLY = 0.85

# Below this a match is discarded rather than queued: too weak to be
# worth a human's time.
DEFAULT_REVIEW_FLOOR = 0.45

# Fields whose value is decided by agreement across sources rather than
# by precedence alone. These are the ones sources genuinely disagree
# about; nobody disputes a title the way they dispute an album.
VOTED_FIELDS = ("album", "album_artist", "year", "release_date")

# How a field's values are compared when voting. Albums need edition
# suffixes stripped first, or sources that agree on the record split
# their vote between "X" and "X (Deluxe Edition)".
_VOTE_KEYS = {"album": norm.release_key}


class MetadataSourceProtocol(Protocol):
  """The part of a metadata source the matcher needs.

  Stated as a protocol rather than importing the concrete base class so
  the matcher can be tested with plain stubs.
  """

  name: str

  def is_available(self) -> bool:
    """Returns whether the source has the credentials it needs."""

  def search(self, query: SourceQuery, limit: int = 3) -> list[SourceResult]:
    """Returns candidate matches for a track."""


class MatchStatus:
  """The outcome states a track can be left in, matching the schema."""
  MATCHED = "matched"
  REVIEW = "review"
  NO_MATCH = "no_match"


@dataclasses.dataclass(frozen=True)
class SourceCandidate:
  """The best candidate one source offered, and how well it scored."""
  result: SourceResult
  score: scoring.Score


@dataclasses.dataclass(frozen=True)
class Match:
  """The proposed metadata for one track.

  Attributes:
    tags: The merged tags, ready for step 6 to write.
    confidence: How much to trust them, 0.0 to 1.0.
    status: One of `MatchStatus`.
    candidates: The best candidate from each source that offered one.
    art_url: The largest cover image any source offered.
    field_sources: Which source each written field came from.
    notes: Things a human reviewing this should know.
  """
  tags: TrackTags
  confidence: float
  status: str
  candidates: Mapping[str, SourceCandidate]
  art_url: str | None = None
  field_sources: Mapping[str, str] = dataclasses.field(default_factory=dict)
  notes: tuple[str, ...] = ()

  def is_empty(self) -> bool:
    """Returns whether no source produced a usable candidate."""
    return not self.candidates


def best_candidate(query: SourceQuery,
                   results: Sequence[SourceResult]) -> SourceCandidate | None:
  """Picks the best of one source's candidates.

  Args:
    query: What is known about the file.
    results: The candidates that source returned.

  Returns:
    The highest-scoring candidate, or None if none cleared
    `MINIMUM_CANDIDATE_SCORE`.
  """
  scored = [
      SourceCandidate(result=result,
                      score=scoring.score_candidate(query, result,
                                                    result.duration_seconds))
      for result in results
  ]
  usable = [
      item for item in scored if item.score.total >= MINIMUM_CANDIDATE_SCORE
  ]
  if not usable:
    return None
  return max(usable, key=lambda item: item.score.total)


def vote(values: Iterable[tuple[str, str]]) -> tuple[str | None, float]:
  """Picks the value the most sources agree on.

  Args:
    values: (source name, value) pairs, already normalised for comparison
      by the caller where that matters.

  Returns:
    The winning value and the share of sources backing it, or
    (None, 0.0) if there were none.
  """
  counts: collections.Counter[str] = collections.Counter()
  for _, value in values:
    counts[value] += 1
  if not counts:
    return (None, 0.0)
  winner, count = counts.most_common(1)[0]
  return (winner, count / sum(counts.values()))


def merge(
    query: SourceQuery, candidates: Mapping[str, SourceCandidate],
    order_for_field: "OrderResolver"
) -> tuple[TrackTags, dict[str, str], list[str]]:
  """Combines candidates into one set of tags.

  Args:
    query: What is known about the file.
    candidates: Best candidate per source.
    order_for_field: Returns the source order to use for a given field.

  Returns:
    The merged tags, which source supplied each field, and any notes.
  """
  values: dict[str, object] = {}
  field_sources: dict[str, str] = {}
  notes: list[str] = []

  for field in ALL_FIELDS:
    if field == "source_video_id":
      continue
    available = [(name, candidate.result.tags.as_dict().get(field))
                 for name, candidate in candidates.items()]
    offered = [(name, value) for name, value in available if value is not None]
    if not offered:
      continue

    chosen_source, chosen_value = _by_precedence(offered,
                                                 order_for_field(field))
    if field in VOTED_FIELDS and len(offered) > 1:
      key = _VOTE_KEYS.get(field, str)
      counts = collections.Counter(key(str(value)) for _, value in offered)
      winner, winner_count = counts.most_common(1)[0]
      # A plurality, not a majority. With four sources a 2-1-1 split is
      # only a 0.5 share, and requiring a strict majority there would
      # keep the single source's answer over the two that agree.
      if winner_count > counts[key(str(chosen_value))]:
        backing = [name for name, value in offered if key(str(value)) == winner]
        backing_text = ", ".join(backing)
        replacement = next(
            value for name, value in offered if name == backing[0])
        # Reports the value actually written, not the normalised key the
        # vote was counted on.
        notes.append(f"{field}: took {replacement!r} from {backing_text}"
                     f" over {chosen_value!r} from {chosen_source}")
        chosen_source = backing[0]
        chosen_value = replacement

    values[field] = chosen_value
    field_sources[field] = chosen_source

  del query
  return (TrackTags.from_mapping(values), field_sources, notes)


def _by_precedence(offered: Sequence[tuple[str, object]],
                   order: Sequence[str]) -> tuple[str, object]:
  """Picks a value by configured source precedence.

  Args:
    offered: (source name, value) pairs that have a value.
    order: Source names in the order they should be preferred.

  Returns:
    The chosen source and value. Falls back to the first offer when no
    configured source supplied one.
  """
  by_source = dict(offered)
  for name in order:
    if name in by_source:
      return (name, by_source[name])
  return offered[0]


class OrderResolver:
  """Resolves the source order for a field, given a detected genre."""

  def __init__(self,
               precedence: PrecedenceConfig,
               genre: str | None = None) -> None:
    """Binds a precedence config to one track's genre.

    Args:
      precedence: The loaded precedence configuration.
      genre: The track's locally-detected genre, if any.
    """
    self._precedence = precedence
    self._genre = genre

  def __call__(self, field: str) -> tuple[str, ...]:
    """Returns the source order for one field.

    Args:
      field: The field name.

    Returns:
      Source names, most preferred first.
    """
    return self._precedence.order_for(self._genre, field)


def confidence_for(candidates: Mapping[str, SourceCandidate],
                   agreement: float) -> float:
  """Scores how much to trust a merged match.

  Blends how well the best candidates scored with how much the sources
  agreed. One source alone scoring highly is weaker evidence than three
  sources scoring well and saying the same thing.

  Args:
    candidates: Best candidate per source.
    agreement: Share of sources agreeing on the core identifying fields.

  Returns:
    0.0 to 1.0.
  """
  if not candidates:
    return 0.0
  scores = [item.score.total for item in candidates.values()]
  best = max(scores)
  breadth = min(len(candidates) / 3.0, 1.0)
  return round(best * 0.6 + agreement * 0.25 + breadth * 0.15, 3)


def core_agreement(candidates: Mapping[str, SourceCandidate]) -> float:
  """Measures how much the sources agree about what this track is.

  Covers title and artist — is this the same recording — and album, which
  is the release it should be tagged from. Album is included because
  total disagreement there is exactly the case that must not be applied
  automatically: every source can name the same song while naming four
  different records, and only this number reveals it.

  Args:
    candidates: Best candidate per source.

  Returns:
    The mean share of sources agreeing per field, or 1.0 when only one
    source answered — nothing disagreed with it.
  """
  if len(candidates) < 2:
    return 1.0
  keys = {
      "title": norm.normalize,
      "artist": norm.normalize,
      "album": norm.release_key,
  }
  shares = []
  for field, key in keys.items():
    values = [(name, key(getattr(candidate.result.tags, field)))
              for name, candidate in candidates.items()]
    values = [(name, value) for name, value in values if value]
    if values:
      shares.append(vote(values)[1])
  return sum(shares) / len(shares) if shares else 0.0


def status_for(confidence: float, auto_apply: float,
               review_floor: float) -> str:
  """Turns a confidence into an outcome.

  Args:
    confidence: The match confidence.
    auto_apply: At or above this, the match is trusted outright.
    review_floor: At or above this but below `auto_apply`, it goes to the
      review queue rather than being discarded.

  Returns:
    One of `MatchStatus`.
  """
  if confidence >= auto_apply:
    return MatchStatus.MATCHED
  if confidence >= review_floor:
    return MatchStatus.REVIEW
  return MatchStatus.NO_MATCH


def largest_art(candidates: Mapping[str, SourceCandidate],
                order: Sequence[str]) -> str | None:
  """Picks a cover image, preferring the biggest on offer.

  Args:
    candidates: Best candidate per source.
    order: Source precedence, used to break ties between equal sizes.

  Returns:
    An image URL, or None if no source offered one.
  """
  offers = [(candidate.result.art_size or
             0, -_rank(name, order), candidate.result.art_url)
            for name, candidate in candidates.items()
            if candidate.result.art_url]
  if not offers:
    return None
  return max(offers)[2]


def _rank(name: str, order: Sequence[str]) -> int:
  """Returns a source's position in a precedence order.

  Args:
    name: The source name.
    order: The precedence order.

  Returns:
    Its index, or a large number if it is not listed.
  """
  return order.index(name) if name in order else len(order)


def match_track(query: SourceQuery,
                sources: Sequence[MetadataSourceProtocol],
                precedence: PrecedenceConfig,
                *,
                genre: str | None = None,
                auto_apply: float = DEFAULT_AUTO_APPLY,
                review_floor: float = DEFAULT_REVIEW_FLOOR) -> Match:
  """Finds and merges metadata for one track.

  Sources are asked in the order the detected genre's precedence gives,
  each for several candidates, and the best-scoring candidate per source
  is kept. A source that fails is skipped rather than ending the match.

  Args:
    query: What is known about the file.
    sources: The sources to ask.
    precedence: The loaded precedence configuration.
    genre: The track's locally-detected genre, which selects the
      precedence ordering.
    auto_apply: Confidence at or above which a match is trusted outright.
    review_floor: Confidence below which a match is discarded rather than
      queued for review.

  Returns:
    The proposed match. Its status is `no_match` when nothing usable was
    found.
  """
  resolver = OrderResolver(precedence, genre)
  default_order = resolver("title")
  ranked = sorted(sources, key=lambda source: _rank(source.name, default_order))

  candidates: dict[str, SourceCandidate] = {}
  notes: list[str] = []
  for source in ranked:
    if not source.is_available():
      continue
    try:
      results = source.search(query, limit=CANDIDATES_PER_SOURCE)
    except SourceError as err:
      # One source being down must not cost the match; the others still
      # have something to say. Only *expected* source failures are
      # swallowed here — anything else is a bug in an adapter and should
      # surface rather than quietly reduce the evidence.
      notes.append(f"{source.name} failed: {err}")
      continue
    chosen = best_candidate(query, results)
    if chosen is not None:
      candidates[source.name] = chosen

  if not candidates:
    return Match(tags=TrackTags(),
                 confidence=0.0,
                 status=MatchStatus.NO_MATCH,
                 candidates={},
                 notes=tuple(notes))

  tags, field_sources, merge_notes = merge(query, candidates, resolver)
  agreement = core_agreement(candidates)
  confidence = confidence_for(candidates, agreement)
  if tags.isrc is None:
    merge_notes.append("no ISRC from any source; left blank for follow-up")
  return Match(tags=tags,
               confidence=confidence,
               status=status_for(confidence, auto_apply, review_floor),
               candidates=candidates,
               art_url=largest_art(candidates, default_order),
               field_sources=field_sources,
               notes=tuple(notes + merge_notes))
