"""Choosing among the results a source returns (SPEC.md §12)."""

import pytest

from music.identify import (
  PLAUSIBLE,
  Evidence,
  Match,
  artist_is_unrelated,
  confidence,
  is_plausible,
  match_score,
  should_auto_accept,
)
from music.sources.base import Identity, best_result


def itunes(artist, title):
  return {"artistName": artist, "trackName": title}


def pick(query_artist, query_title, results):
  return best_result(
    Identity(artist=query_artist, title=query_title),
    results,
    artist=lambda r: r["artistName"],
    title=lambda r: r["trackName"],
  )


# --- ranking ---------------------------------------------------------------


def test_the_plain_recording_beats_a_re_recording():
  """Taylor's Version came back first from iTunes and the original at position 4."""
  got = pick(
    "Taylor Swift",
    "Love Story",
    [
      itunes("Taylor Swift", "Love Story (Taylor's Version)"),
      itunes("Taylor Swift", "Love Story (Live)"),
      itunes("Taylor Swift", "Love Story"),
    ],
  )
  assert got["trackName"] == "Love Story"


def test_a_different_song_at_position_zero_is_not_taken():
  """A `Beauty And A Beat` query came back from iTunes with `Baby` first."""
  got = pick(
    "Justin Bieber",
    "Beauty And A Beat",
    [
      itunes("Justin Bieber", "Baby (feat. Ludacris)"),
      itunes("Justin Bieber", "Beauty and a Beat (feat. Nicki Minaj)"),
    ],
  )
  assert got["trackName"].startswith("Beauty and a Beat")


def test_a_feat_credit_is_not_treated_as_an_unwanted_variant():
  """A featured credit names the same recording; a remix names another."""
  got = pick(
    "Charlie Puth",
    "Marvin Gaye",
    [
      itunes("Charlie Puth", "Marvin Gaye (Remix)"),
      itunes("Charlie Puth", "Marvin Gaye (feat. Meghan Trainor)"),
    ],
  )
  assert "Meghan Trainor" in got["trackName"]


def test_a_channel_name_does_not_veto_a_perfect_title():
  """The query artist is often the uploader, not the performer."""
  got = pick(
    "jayseanworldwide",
    "Down",
    [itunes("Various Artists", "Down Under"), itunes("Jay Sean", "Down")],
  )
  assert got["artistName"] == "Jay Sean"


def test_ties_keep_the_sources_own_order():
  """A source that already ranks well must never be made worse."""
  results = [itunes("A", "Same Title"), itunes("A", "Same Title")]
  assert pick("A", "Same Title", results) is results[0]


def test_no_results_is_none():
  assert pick("A", "B", []) is None


# --- plausibility ----------------------------------------------------------


@pytest.mark.parametrize(
  ("query", "got"),
  [
    (("Morgan Wallen", "Last Night"), ("Metro Station", "California")),
    (("Beyonce", "Halo"), ("Deborah Cox", "Nobody's Supposed to Be Here")),
    (("Owl City", "Fireflies"), ("Vanilla Ice", "Ice Ice Baby")),
    (("Avicii", "Waiting For Love"), ("Juice WRLD", "The Party Never Ends")),
  ],
)
def test_the_real_misidentifications_are_implausible(query, got):
  """Four of the twelve tracks published as the wrong song."""
  assert not is_plausible(*query, *got)


@pytest.mark.parametrize(
  ("query", "got"),
  [
    (("Morgan Wallen", "Last Night"), ("Morgan Wallen", "Last Night")),
    (("jayseanworldwide", "Down"), ("Jay Sean", "Down")),
    (("iyazlive", "Replay"), ("Iyaz", "Replay")),
    (("push baby", "Me And My Broken Heart"), ("Rixton", "Me and My Broken Heart")),
    (("Train", "Hey, Soul Sister"), ("Train", "Hey, Soul Sister (Country Mix)")),
  ],
)
def test_correct_matches_stay_plausible(query, got):
  """Including the ones where the query artist was a channel name."""
  assert is_plausible(*query, *got)


def test_a_cover_is_not_caught_by_plausibility():
  """Documented limit: a cover carries the right title.

  Ranking handles this — the original outscores the cover — but the gate
  alone will not reject it, and pretending otherwise would be worse than
  saying so.
  """
  assert (
    match_score(
      "Taylor Swift",
      "We Are Never Ever Getting Back Together",
      "Luke Conard",
      "We Are Never Ever Getting Back Together",
    )
    > PLAUSIBLE
  )


def test_the_original_outranks_the_cover():
  got = pick(
    "Taylor Swift",
    "We Are Never Ever Getting Back Together",
    [
      itunes("Luke Conard", "We Are Never Ever Getting Back Together"),
      itunes("Taylor Swift", "We Are Never Ever Getting Back Together"),
    ],
  )
  assert got["artistName"] == "Taylor Swift"


# --- covers ----------------------------------------------------------------


@pytest.mark.parametrize(
  ("query", "matched"),
  [
    ("Avicii", "Die NotenDealer"),
    ("Taylor Swift", "Luke Conard"),
    ("Morgan Wallen", "Metro Station"),
    ("Beyonce", "Deborah Cox"),
  ],
)
def test_a_stranger_credit_is_flagged(query, matched):
  """A fingerprint match carrying the right title and a stranger's name.

  Title scoring cannot see this, so it is checked separately and used only to
  hold confidence down — never to filter a result out.
  """
  assert artist_is_unrelated(query, matched)


@pytest.mark.parametrize(
  ("query", "matched"),
  [
    ("jayseanworldwide", "Jay Sean"),
    ("iyazlive", "Iyaz"),
    ("seankingston", "Sean Kingston"),
    ("Queen Official", "Queen"),
    ("The Kid LAROI.", "The Kid LAROI"),
    ("JAŸ-Z", "Jay‐Z"),
    ("Black Eyed Peas", "The Black Eyed Peas"),
    ("kesha", "Ke$ha"),
    ("will i am", "will.i.am"),
    ("Anne-Marie", "Anne‐Marie"),
    ("MKTO Band", "MKTO"),
  ],
)
def test_channel_names_and_stylings_are_not_flagged(query, matched):
  """Every real query/match pair from the first hundred-track run."""
  assert not artist_is_unrelated(query, matched)


def test_a_renamed_band_is_flagged_and_that_is_the_safe_direction():
  """`push baby` is Rixton. This costs one review item.

  The alternative — relaxing until a renamed band passes — lets a cover
  through, and a cover published as the original is silent and permanent.
  """
  assert artist_is_unrelated("push baby", "Rixton")


def test_a_cover_no_longer_auto_accepts():
  match = Match(
    evidence=Evidence.ACOUSTID,
    acoustid_score=0.99,
    duration_delta_s=0.0,
    artist_unrelated=True,
  )
  assert confidence(match) == 0.50
  assert not should_auto_accept(match)


def test_a_genuine_fingerprint_match_still_auto_accepts():
  match = Match(evidence=Evidence.ACOUSTID, acoustid_score=0.99, duration_delta_s=0.0)
  assert confidence(match) == 0.95
  assert should_auto_accept(match)
