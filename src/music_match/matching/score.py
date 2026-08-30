"""Scoring how well a source's candidate matches the track in hand.

Every source ranks results for something other than what this tool wants.
Spotify ranks by popularity, iTunes by store relevance, MusicBrainz by
text score over a catalogue thick with live recordings, and Discogs
indexes physical releases rather than tracks. Measured against tracks with
known-correct albums, simply taking each platform's first result is right
about half the time, and the failures are systematic: soundtracks, live
albums and greatest-hits compilations.

So candidates are scored here rather than trusted. The signals, strongest
first:

- **Duration.** A live cut runs minutes longer than the studio take, so
  this separates the two cases that string matching cannot. `scan` has
  already measured it for every file.
- **Title and artist similarity**, after normalisation.
- **Penalties** for the album shapes that are usually the wrong answer,
  unless the track being matched is itself asking for one.
"""

import dataclasses
import difflib
from typing import Mapping

from music_match.matching import normalize as norm
from music_match.sources.base import SourceQuery
from music_match.sources.base import SourceResult

# Weights over the signals that are present. They are renormalised when a
# signal is missing, so a file with no known duration is scored on title
# and artist alone rather than being capped below every threshold.
WEIGHT_DURATION = 0.35
WEIGHT_TITLE = 0.40
WEIGHT_ARTIST = 0.25

# Durations this close count as identical; beyond the second value the
# duration signal is zero. Encoders and sources disagree by a second or
# two routinely.
DURATION_EXACT_SECONDS = 3.0
DURATION_LIMIT_SECONDS = 20.0

# Album titles that usually mean the wrong release was matched. Each
# penalty is subtracted from the final score.
ALBUM_PENALTIES: Mapping[str, float] = {
    "greatest hits": 0.25,
    "best of": 0.20,
    "compilation": 0.20,
    "soundtrack": 0.20,
    "motion picture": 0.20,
    "karaoke": 0.45,
    "tribute": 0.45,
    "made popular by": 0.45,
    "in the style of": 0.45,
    "live at": 0.30,
    "live in": 0.30,
    "live from": 0.30,
    "unplugged": 0.20,
    "remixes": 0.25,
    "the remixes": 0.25,
}

# A title saying "live" or "remix" is a different recording, not a worse
# match — penalise only when the query did not ask for one.
VARIANT_MARKERS = ("live", "remix", "acoustic", "instrumental", "karaoke",
                   "demo", "edit", "version", "cover")


@dataclasses.dataclass(frozen=True)
class Score:
  """How well one candidate matches, and why.

  Attributes:
    total: The overall score, 0.0 to 1.0.
    duration: The duration component, or None if no duration was known.
    title: The title similarity component.
    artist: The artist similarity component.
    penalty: How much was subtracted for suspicious album or title shapes.
    reasons: Human-readable notes on what was penalised.
  """
  total: float
  duration: float | None
  title: float
  artist: float
  penalty: float
  reasons: tuple[str, ...] = ()

  def describe(self) -> str:
    """Returns a one-line breakdown for CLI output.

    Returns:
      The component scores, with the duration omitted when unknown.
    """
    duration = "n/a" if self.duration is None else f"{self.duration:.2f}"
    return (f"total {self.total:.2f} (title {self.title:.2f} "
            f"artist {self.artist:.2f} duration {duration} "
            f"penalty {self.penalty:.2f})")


def similarity(left: str | None, right: str | None) -> float:
  """Compares two strings after normalising both.

  Blends a sequence ratio with token overlap: the ratio catches small
  spelling differences, the token overlap survives reordering like
  "Vishal-Shekhar, Benny Dayal" against "Benny Dayal, Vishal-Shekhar".

  Args:
    left: One value.
    right: The other.

  Returns:
    0.0 to 1.0, and 0.0 if either side is empty.
  """
  first, second = norm.normalize(left), norm.normalize(right)
  if not first or not second:
    return 0.0
  if first == second:
    return 1.0
  ratio = difflib.SequenceMatcher(None, first, second).ratio()
  left_tokens, right_tokens = norm.tokens(left), norm.tokens(right)
  overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
  return max(ratio, overlap * 0.5 + ratio * 0.5)


def duration_score(wanted: float | None, found: float | None) -> float | None:
  """Scores how closely two durations agree.

  Args:
    wanted: The file's duration in seconds, if known.
    found: The candidate's duration in seconds, if the source gave one.

  Returns:
    1.0 for a near-exact match, falling linearly to 0.0 at
    `DURATION_LIMIT_SECONDS` apart, or None if either is unknown.
  """
  if not wanted or not found:
    return None
  gap = abs(wanted - found)
  if gap <= DURATION_EXACT_SECONDS:
    return 1.0
  if gap >= DURATION_LIMIT_SECONDS:
    return 0.0
  span = DURATION_LIMIT_SECONDS - DURATION_EXACT_SECONDS
  return 1.0 - (gap - DURATION_EXACT_SECONDS) / span


def album_penalty(album: str | None,
                  query_title: str | None) -> tuple[float, tuple[str, ...]]:
  """Penalises album titles that usually mean the wrong release.

  A soundtrack or greatest-hits appearance is rarely the release worth
  tagging from, but it is exactly what a text search surfaces first.

  Args:
    album: The candidate's album title.
    query_title: The title being searched for, so a track that *is* a
      live recording is not penalised for matching one.

  Returns:
    The total penalty and the phrases that caused it.
  """
  if not album:
    return (0.0, ())
  normalized = norm.normalize(album)
  asked = norm.tokens(query_title)
  penalty = 0.0
  reasons = []
  for phrase, cost in ALBUM_PENALTIES.items():
    if phrase not in normalized:
      continue
    # A track that is itself a live cut or a remix should not be
    # penalised for matching a live album or a remixes EP — that is the
    # right release for it.
    if any(word in asked for word in phrase.split()):
      continue
    penalty += cost
    reasons.append(f"album looks like a {phrase} release")
  return (penalty, tuple(reasons))


def variant_penalty(
    query_title: str | None,
    candidate_title: str | None,
    duration: float | None = None) -> tuple[float, tuple[str, ...]]:
  """Penalises a candidate that is a different version of the same song.

  "Strobe" and "Strobe (DJ Marky Remix)" are different recordings. Tagging
  one as the other is a worse outcome than not matching at all, so a
  variant marker on only one side costs the candidate.

  Unless the durations agree closely, in which case it does not. A file
  named "Get Lucky" that runs exactly as long as a source's
  "Get Lucky (Radio Edit)" *is* the radio edit — the file simply was not
  labelled as one. Duration is physical evidence about the recording; a
  title marker is a labelling convention, and the evidence wins.

  Args:
    query_title: The title being searched for.
    candidate_title: The candidate's title.
    duration: The duration component of this candidate's score, if a
      duration was known for both sides.

  Returns:
    The penalty and the markers that caused it.
  """
  if duration is not None and duration >= 0.9:
    return (0.0, ())
  asked = norm.tokens(query_title)
  found = norm.tokens(candidate_title)
  mismatched = [
      marker for marker in VARIANT_MARKERS
      if (marker in found) != (marker in asked)
  ]
  if not mismatched:
    return (0.0, ())
  return (0.25 * len(mismatched),
          tuple(f"one side is a {marker}" for marker in mismatched))


def score_candidate(query: SourceQuery,
                    candidate: SourceResult,
                    candidate_duration: float | None = None) -> Score:
  """Scores one candidate against the track being matched.

  Args:
    query: What is known about the file.
    candidate: The candidate returned by a source.
    candidate_duration: The candidate's duration in seconds, where the
      source reported one.

  Returns:
    The score and its breakdown.
  """
  title = similarity(query.title, candidate.tags.title)
  artist = similarity(query.artist, candidate.tags.artist)
  duration = duration_score(query.duration_seconds, candidate_duration)

  weights = {"title": WEIGHT_TITLE, "artist": WEIGHT_ARTIST}
  parts = {"title": title, "artist": artist}
  if duration is not None:
    weights["duration"] = WEIGHT_DURATION
    parts["duration"] = duration
  if not query.artist:
    # Nothing to compare against, so scoring it as zero would punish a
    # perfectly good candidate for the file's missing tag.
    weights.pop("artist")
    parts.pop("artist")

  total_weight = sum(weights.values())
  weighted = sum(weight * parts[name] for name, weight in weights.items())
  base = weighted / total_weight if total_weight else 0.0

  album_cost, album_reasons = album_penalty(candidate.tags.album, query.title)
  variant_cost, variant_reasons = variant_penalty(query.title,
                                                  candidate.tags.title,
                                                  duration)
  penalty = album_cost + variant_cost
  return Score(total=max(0.0, min(1.0, base - penalty)),
               duration=duration,
               title=title,
               artist=artist,
               penalty=penalty,
               reasons=album_reasons + variant_reasons)
