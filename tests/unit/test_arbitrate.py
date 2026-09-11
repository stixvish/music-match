"""Arbitration (tasks/todo.md t24, SPEC.md §7 and §12)."""

import pytest

from music.arbitrate import Decision, arbitrate, precedence_for
from music.sources.base import FieldCandidate


def cand(field, value, source):
  return FieldCandidate(field=field, value=value, source=source)


def resolved(decisions):
  return {d.field: (d.value, d.source) for d in decisions}


# --- ranking ---------------------------------------------------------------


def test_first_ranked_source_wins():
  got = resolved(
    arbitrate(
      [cand("genre", "Pop", "musicbrainz"), cand("genre", "Deep House", "discogs")],
      family="pop",
    )
  )
  assert got["genre"] == ("Deep House", "discogs")


def test_lower_ranked_source_fills_a_gap():
  got = resolved(arbitrate([cand("genre", "Pop", "musicbrainz")], family="pop"))
  assert got["genre"] == ("Pop", "musicbrainz")


def test_beatport_leads_genre_for_electronic_only():
  assert precedence_for("electronic", "genre")[0] == "beatport"
  assert precedence_for("pop", "genre")[0] == "discogs"


def test_itunes_leads_for_world():
  """ITunes has the strongest catalogue for regional music (SPEC.md §7)."""
  for field in ("title", "artist", "album", "genre"):
    assert precedence_for("world", field)[0] == "itunes"


def test_unranked_field_still_gets_a_value():
  got = resolved(arbitrate([cand("weird_field", "x", "somewhere")]))
  assert got["weird_field"] == ("x", "somewhere")


def test_empty_values_are_ignored():
  got = resolved(
    arbitrate([cand("genre", "", "discogs"), cand("genre", "House", "musicbrainz")])
  )
  assert got["genre"] == ("House", "musicbrainz")


def test_no_candidates():
  assert arbitrate([]) == []


# --- album group atomicity (SPEC.md §7) ------------------------------------


def test_album_fields_come_from_one_source():
  """Album fields must all come from one source.

  Album name from a deluxe edition with a track number from the standard gives
  track 14 of a 12-track album (SPEC.md §7).
  """
  decisions = arbitrate(
    [
      cand("album", "Planet Pit (Deluxe)", "musicbrainz"),
      cand("track_number", "14", "musicbrainz"),
      cand("album", "Planet Pit", "spotify"),
      cand("track_number", "7", "spotify"),
    ],
    family="pop",
  )
  got = resolved(decisions)
  assert got["album"][1] == got["track_number"][1] == "musicbrainz"
  assert got["album"][0] == "Planet Pit (Deluxe)"
  assert got["track_number"][0] == "14"


def test_album_group_does_not_mix_even_when_one_source_is_partial():
  decisions = arbitrate(
    [
      cand("album", "A", "musicbrainz"),
      cand("track_number", "3", "spotify"),
      cand("album", "B", "spotify"),
    ],
    family="pop",
  )
  got = resolved(decisions)
  # musicbrainz is ranked first and offers an album, so it supplies the group;
  # spotify's track number is not borrowed
  assert got["album"] == ("A", "musicbrainz")
  assert "track_number" not in got


def test_album_group_falls_back_to_an_unranked_source():
  got = resolved(arbitrate([cand("album", "X", "obscure")], family="pop"))
  assert got["album"] == ("X", "obscure")


def test_non_album_fields_are_ranked_independently():
  got = resolved(
    arbitrate(
      [
        cand("album", "A", "musicbrainz"),
        cand("genre", "Techno", "discogs"),
        cand("genre", "Electronic", "musicbrainz"),
      ],
      family="pop",
    )
  )
  assert got["album"][1] == "musicbrainz"
  assert got["genre"] == ("Techno", "discogs")


def test_injected_ranking_is_used():
  got = resolved(
    arbitrate(
      [cand("genre", "A", "one"), cand("genre", "B", "two")],
      ranking=lambda family, field: ("two", "one"),
    )
  )
  assert got["genre"] == ("B", "two")


@pytest.mark.parametrize("field", ["bpm", "key"])
def test_local_analysis_outranks_sources(field):
  """Rekordbox and Serato compute these from the audio itself."""
  assert precedence_for("electronic", field)[0] == "local"


def test_derived_values_outrank_sources_for_version_fields():
  """Remixer and mix_name come from the title we already have."""
  assert precedence_for("pop", "remixer")[0] == "derived"
  assert precedence_for("pop", "mix_name")[0] == "derived"


def test_decision_defaults_to_precedence():
  assert Decision("f", "v", "s").decided_by == "precedence"


# --- dates are chosen by earliest, not by precedence (SPEC.md §7) ----------


def test_earliest_date_wins_regardless_of_source_rank():
  got = resolved(
    arbitrate(
      [
        cand("release_date", "2013-04-06", "spotify"),
        cand("release_date", "2013-03-16", "itunes"),
      ],
      family="pop",
    )
  )
  assert got["release_date"] == ("2013-03-16", "itunes")


def test_more_precise_date_wins_within_the_same_year():
  """A more precise date wins within the same year.

  MusicBrainz returned a bare "2013" while iTunes returned "2013-03-16";
  taking the first-ranked source would lose the precise date.
  """
  got = resolved(
    arbitrate(
      [
        cand("release_date", "2013", "musicbrainz"),
        cand("release_date", "2013-03-16", "itunes"),
      ],
      family="world",
    )
  )
  assert got["release_date"] == ("2013-03-16", "itunes")


def test_an_earlier_year_beats_a_more_precise_later_one():
  got = resolved(
    arbitrate(
      [
        cand("release_date", "2015-01-01", "itunes"),
        cand("release_date", "2013", "musicbrainz"),
      ],
      family="pop",
    )
  )
  assert got["release_date"] == ("2013", "musicbrainz")


def test_year_field_is_also_earliest_wins():
  got = resolved(
    arbitrate(
      [cand("year", "2023", "spotify"), cand("year", "2022", "musicbrainz")],
      family="pop",
    )
  )
  assert got["year"] == ("2022", "musicbrainz")


# --- unranked wins are marked, not hidden ---------------------------------


def test_unranked_source_is_recorded_as_a_fallback():
  """An unranked source that wins is marked as a fallback.

  iTunes is not ranked for electronic genre but won by falling through; that
  must be visible rather than silent.
  """
  decisions = arbitrate([cand("genre", "Dance", "itunes")], family="electronic")
  decision = next(d for d in decisions if d.field == "genre")
  assert (decision.source, decision.decided_by) == ("itunes", "fallback")


def test_a_ranked_source_is_recorded_as_precedence():
  decisions = arbitrate([cand("genre", "House", "discogs")], family="electronic")
  decision = next(d for d in decisions if d.field == "genre")
  assert decision.decided_by == "precedence"
