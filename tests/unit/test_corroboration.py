"""Independent catalogues settling a doubtful identity (SPEC.md §12)."""

import pytest

from music.identify import CORROBORATED, Evidence, Match, confidence, should_auto_accept
from music.resolve import corroborates
from music.sources.base import FieldCandidate, Identity

QUERY = Identity(artist="Carly Rae Jepsen", title="Call Me Maybe")


def cands(*rows):
  return [FieldCandidate(field=f, value=v, source=s) for s, f, v in rows]


def test_two_sources_agreeing_with_the_query_corroborate():
  """The real case: the fingerprint matched Kidz Bop's cover."""
  assert corroborates(
    QUERY,
    cands(
      ("itunes", "artist", "Carly Rae Jepsen"),
      ("itunes", "title", "Call Me Maybe"),
      ("spotify", "artist", "Carly Rae Jepsen"),
      ("spotify", "title", "Call Me Maybe"),
    ),
  )


def test_one_source_alone_does_not_corroborate():
  assert not corroborates(
    QUERY,
    cands(
      ("itunes", "artist", "Carly Rae Jepsen"), ("itunes", "title", "Call Me Maybe")
    ),
  )


def test_sources_disagreeing_with_each_other_do_not_corroborate():
  assert not corroborates(
    QUERY,
    cands(
      ("itunes", "artist", "Carly Rae Jepsen"),
      ("itunes", "title", "Call Me Maybe"),
      ("spotify", "artist", "Kidz Bop Kids"),
      ("spotify", "title", "Call Me Maybe"),
    ),
  )


def test_sources_agreeing_on_the_wrong_song_do_not_corroborate():
  """Two catalogues describing the same wrong song must not confirm each other."""
  assert not corroborates(
    QUERY,
    cands(
      ("itunes", "artist", "Metro Station"),
      ("itunes", "title", "California"),
      ("spotify", "artist", "Metro Station"),
      ("spotify", "title", "California"),
    ),
  )


def test_sources_agreeing_on_a_cover_do_not_corroborate():
  """The right title with a stranger's name is the cover signature."""
  assert not corroborates(
    QUERY,
    cands(
      ("itunes", "artist", "Kidz Bop"),
      ("itunes", "title", "Call Me Maybe"),
      ("spotify", "artist", "Kidz Bop"),
      ("spotify", "title", "Call Me Maybe"),
    ),
  )


def test_a_version_qualifier_does_not_block_agreement():
  """One catalogue listing a feat. credit is not a disagreement about the song."""
  assert corroborates(
    QUERY,
    cands(
      ("itunes", "artist", "Carly Rae Jepsen"),
      ("itunes", "title", "Call Me Maybe (Radio Edit)"),
      ("spotify", "artist", "Carly Rae Jepsen"),
      ("spotify", "title", "Call Me Maybe"),
    ),
  )


def test_a_missing_artist_or_title_is_not_a_vote():
  assert not corroborates(
    QUERY,
    cands(
      ("itunes", "title", "Call Me Maybe"),
      ("spotify", "title", "Call Me Maybe"),
    ),
  )


# --- effect on confidence --------------------------------------------------


def test_corroboration_cannot_lift_a_cover():
  """The correction that matters most here.

  A fingerprint is acoustic evidence about *this file*; catalogue agreement is
  bibliographic evidence about the song. Two catalogues confirming that Taylor
  Swift recorded the track does not make this file her recording of it. Before
  this guard, three covers were lifted straight back to auto-accept and would
  have been published under the original artist's name.
  """
  match = Match(
    evidence=Evidence.ACOUSTID,
    acoustid_score=0.99,
    duration_delta_s=0.0,
    artist_unrelated=True,
    corroborated=True,
  )
  assert confidence(match) == 0.50
  assert not should_auto_accept(match)


def test_corroboration_lifts_a_weak_fingerprint():
  """What it is for: doubt about the match, not about the audio."""
  match = Match(
    evidence=Evidence.ACOUSTID,
    acoustid_score=0.55,
    duration_delta_s=0.0,
    corroborated=True,
  )
  assert confidence(match) == CORROBORATED
  assert should_auto_accept(match)


def test_corroboration_lifts_a_duration_mismatch():
  match = Match(
    evidence=Evidence.ACOUSTID,
    acoustid_score=0.99,
    duration_delta_s=9.0,
    corroborated=True,
  )
  assert should_auto_accept(match)


def test_corroboration_cannot_lift_a_version_mismatch():
  """Agreeing on the song says nothing about which cut this file holds."""
  match = Match(
    evidence=Evidence.ACOUSTID,
    acoustid_score=0.99,
    duration_delta_s=0.0,
    variant_mismatch=True,
    corroborated=True,
  )
  assert confidence(match) == 0.50
  assert not should_auto_accept(match)


def test_corroboration_never_lowers_a_clean_match():
  clean = Match(evidence=Evidence.ACOUSTID, acoustid_score=0.99, duration_delta_s=0.0)
  assert confidence(clean) == 0.95
  assert confidence(Match(**{**clean.__dict__, "corroborated": True})) == 0.95


@pytest.mark.parametrize("evidence", [Evidence.NONE])
def test_corroboration_does_not_rescue_no_match(evidence):
  assert confidence(Match(evidence=evidence, corroborated=True)) == 0.0
