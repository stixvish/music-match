"""Confidence scoring (tasks/todo.md t11, SPEC.md §12)."""

import pytest

from music.identify import (
  AUTO_ACCEPT,
  Evidence,
  Match,
  confidence,
  normalised_equal,
  review_reason,
  should_auto_accept,
  variant_mismatch,
)


@pytest.mark.parametrize(
  ("match", "expected"),
  [
    (Match(Evidence.URL_OVERRIDE), 1.00),
    (Match(Evidence.ISRC), 0.98),
    (Match(Evidence.ACOUSTID, acoustid_score=0.95, duration_delta_s=1.0), 0.95),
    (Match(Evidence.ACOUSTID, acoustid_score=0.80, duration_delta_s=1.0), 0.50),
    (Match(Evidence.ACOUSTID, acoustid_score=0.95, duration_delta_s=30.0), 0.50),
    (Match(Evidence.TEXT, search_score=100, duration_delta_s=1.0, rival_gap=10), 0.85),
    (Match(Evidence.TEXT, search_score=100, duration_delta_s=40.0, rival_gap=10), 0.50),
    (Match(Evidence.TEXT, search_score=100, duration_delta_s=1.0, rival_gap=0), 0.50),
    (Match(Evidence.TEXT, search_score=70, duration_delta_s=1.0, rival_gap=10), 0.50),
    (Match(Evidence.NONE), 0.00),
  ],
)
def test_confidence_table_matches_spec(match, expected):
  assert confidence(match) == expected


def test_threshold_splits_auto_from_review():
  assert should_auto_accept(Match(Evidence.ISRC))
  assert not should_auto_accept(Match(Evidence.TEXT, search_score=70))
  assert AUTO_ACCEPT == 0.80


def test_variant_mismatch_drops_below_threshold():
  """An unrequested version qualifier must drop below auto-accept.

  Regression: a text match returned "SMASH! (instrumental)" at full confidence
  with a matching duration (SPEC.md §4).
  """
  clean = Match(Evidence.TEXT, search_score=100, duration_delta_s=0.5, rival_gap=10)
  assert should_auto_accept(clean)
  suspect = Match(
    Evidence.TEXT,
    search_score=100,
    duration_delta_s=0.5,
    rival_gap=10,
    variant_mismatch=True,
  )
  assert not should_auto_accept(suspect)


def test_variant_mismatch_cannot_promote_a_weak_match():
  assert confidence(Match(Evidence.NONE, variant_mismatch=True)) == 0.0


@pytest.mark.parametrize(
  ("query", "matched", "expected"),
  [
    ("SMASH!", "SMASH! (instrumental)", True),
    ("SMASH!", "SMASH!", False),
    ("Song (Live)", "Song (Live)", False),  # asked for live, got live
    ("Song", "Song - Live at Wembley", True),
    ("Song", "Song (Sped Up)", True),
    ("Song (Radio Edit)", "Song (Radio Edit)", False),
    ("Delilah", "Delilah (Tom Santa Remix)", False),  # remix is not a variant word
  ],
)
def test_variant_mismatch(query, matched, expected):
  assert variant_mismatch(query, matched) is expected


def test_review_reason():
  assert review_reason(Match(Evidence.ISRC)) is None
  assert review_reason(Match(Evidence.NONE)) == "no_match"
  assert review_reason(Match(Evidence.TEXT, search_score=50)) == "low_confidence"


@pytest.mark.parametrize(
  ("a", "b", "same"),
  [
    ("Don't Stop", "Dont Stop", True),
    ("Hello  World", "hello world", True),
    ("Mood", "Mood (ft. iann dior)", False),
  ],
)
def test_normalised_equal(a, b, same):
  assert normalised_equal(a, b) is same
