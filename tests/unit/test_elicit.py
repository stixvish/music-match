"""Elicitation: blind comparison and rank derivation (tasks/todo.md t31)."""

import random

import pytest

from music.elicit import (
  MIN_OBSERVATIONS,
  Observation,
  Tally,
  build_question,
  derive,
  rank_cell,
  tally,
)
from music.sources.base import FieldCandidate


def cand(field, value, source):
  return FieldCandidate(field=field, value=value, source=source)


# --- question construction -------------------------------------------------


def test_agreeing_sources_share_one_option():
  """Two sources with the same value is not a question with two answers."""
  q = build_question(
    1,
    "pop",
    "title",
    [
      cand("title", "Give Me Everything", "musicbrainz"),
      cand("title", "give me everything", "spotify"),
      cand("title", "Give Me Everything (feat. Nayer)", "itunes"),
    ],
  )
  assert q is not None
  assert len(q.options) == 2
  merged = next(o for o in q.options if len(o.sources) == 2)
  assert merged.sources == ("musicbrainz", "spotify")


def test_unanimous_agreement_is_not_asked():
  assert (
    build_question(
      1,
      "pop",
      "title",
      [cand("title", "Summer", "musicbrainz"), cand("title", "Summer", "spotify")],
    )
    is None
  )


def test_a_single_source_is_not_asked():
  assert (
    build_question(1, "pop", "label", [cand("label", "Mr. 305", "discogs")]) is None
  )


def test_non_elicitable_fields_are_never_asked():
  """Dates are decided by `_earliest`, so a preference would never be used."""
  for skipped in ("release_date", "year", "isrc", "bpm", "album_artist"):
    assert (
      build_question(
        1,
        "pop",
        skipped,
        [cand(skipped, "a", "musicbrainz"), cand(skipped, "b", "spotify")],
      )
      is None
    ), skipped


def test_album_question_carries_the_whole_group():
  """The album group resolves atomically, so it is compared atomically."""
  q = build_question(
    1,
    "pop",
    "album",
    [
      cand("album", "Planet Pit", "musicbrainz"),
      cand("track_number", "9", "musicbrainz"),
      cand("album", "Planet Pit (Deluxe Version)", "spotify"),
      cand("track_number", "2", "spotify"),
    ],
  )
  assert q is not None
  assert len(q.options) == 2
  numbers = {dict(o.extra)["track"] for o in q.options}
  assert numbers == {"9", "2"}


def test_album_editions_with_identical_numbering_are_one_option():
  q = build_question(
    1,
    "pop",
    "album",
    [
      cand("album", "Planet Pit", "musicbrainz"),
      cand("track_number", "2", "musicbrainz"),
      cand("album", "planet pit", "spotify"),
      cand("track_number", "2", "spotify"),
    ],
  )
  assert q is None


def test_option_order_is_shuffled_not_source_order():
  """Presentation order must not leak the ranking (SPEC.md §9)."""
  options = [
    cand("genre", "House", "discogs"),
    cand("genre", "Dance", "itunes"),
    cand("genre", "Electronic", "essentia"),
  ]
  seen = set()
  for seed in range(30):
    q = build_question(1, "pop", "genre", options, rng=random.Random(seed))
    seen.add(tuple(o.sources[0] for o in q.options))
  assert len(seen) > 1


def test_question_never_names_its_sources_in_the_value():
  q = build_question(
    1,
    "pop",
    "genre",
    [cand("genre", "House", "discogs"), cand("genre", "Dance", "itunes")],
  )
  assert {o.value for o in q.options} == {"House", "Dance"}


# --- tallying --------------------------------------------------------------


def test_every_offered_source_is_counted_as_seen():
  counts = tally(
    [Observation("pop", "genre", ("discogs",), ("discogs", "itunes", "essentia"))]
  )
  cell = counts[("pop", "genre")]
  assert cell["discogs"] == Tally(wins=1, seen=1)
  assert cell["itunes"] == Tally(wins=0, seen=1)


def test_no_preference_records_appearances_without_a_win():
  """Seeing no difference is evidence that neither source is better."""
  counts = tally([Observation("pop", "genre", (), ("discogs", "itunes"))])
  assert all(t.wins == 0 and t.seen == 1 for t in counts[("pop", "genre")].values())


def test_agreeing_sources_both_win():
  counts = tally(
    [
      Observation(
        "pop", "title", ("musicbrainz", "spotify"), ("musicbrainz", "spotify", "itunes")
      )
    ]
  )
  cell = counts[("pop", "title")]
  assert cell["musicbrainz"].wins == 1
  assert cell["spotify"].wins == 1
  assert cell["itunes"].wins == 0


# --- ranking ---------------------------------------------------------------


def test_thin_evidence_does_not_overwrite_the_default():
  """One or two answers is noise; the built-in ranking is better than noise."""
  counts = {"itunes": Tally(wins=2, seen=2), "discogs": Tally(wins=0, seen=2)}
  assert rank_cell(counts, ("discogs", "itunes")) == ()


def test_no_evidence_reproduces_the_default_exactly():
  default = ("discogs", "musicbrainz", "itunes", "essentia")
  counts = {s: Tally(wins=0, seen=MIN_OBSERVATIONS) for s in default}
  assert rank_cell(counts, default) == default


def test_a_consistent_winner_overtakes_a_higher_default():
  """The whole point: measured preference beats a reasoned guess."""
  counts = {
    "discogs": Tally(wins=0, seen=8),
    "itunes": Tally(wins=8, seen=8),
  }
  ranked = rank_cell(counts, ("discogs", "musicbrainz", "itunes"))
  assert ranked[0] == "itunes"


def test_a_source_never_shown_keeps_its_default_position():
  """Absence of evidence must not push a source to last place."""
  counts = {"discogs": Tally(wins=5, seen=6), "itunes": Tally(wins=1, seen=6)}
  ranked = rank_cell(counts, ("discogs", "musicbrainz", "itunes"))
  assert ranked.index("musicbrainz") < ranked.index("itunes")


def test_one_lucky_win_does_not_outrank_a_sustained_one():
  counts = {
    "discogs": Tally(wins=6, seen=8),
    "itunes": Tally(wins=1, seen=1),
    "musicbrainz": Tally(wins=1, seen=8),
  }
  ranked = rank_cell(counts, ("discogs", "musicbrainz", "itunes"))
  assert ranked[0] == "discogs"


def test_ranking_keeps_every_source_exactly_once():
  counts = {"itunes": Tally(wins=5, seen=5), "beatport": Tally(wins=0, seen=5)}
  ranked = rank_cell(counts, ("discogs", "musicbrainz"))
  assert sorted(ranked) == sorted({"discogs", "musicbrainz", "itunes", "beatport"})


# --- derivation ------------------------------------------------------------


def test_derive_only_emits_calibrated_cells():
  obs = [Observation("pop", "genre", ("itunes",), ("discogs", "itunes"))] * 6
  obs += [Observation("world", "genre", ("itunes",), ("discogs", "itunes"))]
  out = derive(obs)
  assert ("pop", "genre") in out
  assert ("world", "genre") not in out


def test_derive_skips_date_fields_even_if_somehow_recorded():
  obs = [Observation("pop", "year", ("itunes",), ("discogs", "itunes"))] * 9
  assert derive(obs) == {}


def test_derived_ranking_is_usable_as_a_precedence_tuple():
  obs = [Observation("electronic", "label", ("discogs",), ("discogs", "beatport"))] * 8
  ranked = derive(obs)[("electronic", "label")]
  assert ranked[0] == "discogs"
  assert all(isinstance(s, str) for s in ranked)


@pytest.mark.parametrize("family", ["pop", "electronic", "world", "other"])
def test_every_family_can_be_calibrated(family):
  obs = [Observation(family, "genre", ("itunes",), ("discogs", "itunes"))] * 8
  assert (family, "genre") in derive(obs)
