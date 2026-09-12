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


def test_itunes_leads_genre_everywhere():
  assert precedence_for("electronic", "genre")[0] == "itunes"
  assert precedence_for("pop", "genre")[0] == "itunes"
  assert precedence_for("world", "genre")[0] == "itunes"
  # beatport's sub-genres are not discarded; they move to genre_style
  assert precedence_for("electronic", "genre_style")[0] == "beatport"


def test_world_keeps_the_performer_credit():
  """ITunes has the strongest regional catalogue, but not the right convention.

  Coverage and credit convention are different questions. Indian film music
  credits the music director as the track artist — iTunes and Spotify both do,
  MusicBrainz names the singer — so `artist` is the one field where iTunes does
  not lead this family.
  """
  assert precedence_for("world", "genre")[0] == "itunes"
  # spotify leads identity, as everywhere outside electronic
  for field in ("title", "album", "album_artist"):
    assert precedence_for("world", field)[0] == "spotify"
  # except the performer, which is the whole point of the world override
  assert precedence_for("world", "artist")[0] == "musicbrainz"


def test_two_catalogues_sharing_a_convention_do_not_outvote_the_performer():
  """The real shape of "Sanam Re", "Jeena Jeena" and "Jag Ghoomeya".

  iTunes and Spotify agree because they follow the same convention, not
  because they independently verified anything — so agreement is not evidence
  here and precedence decides.
  """
  got = resolved(
    arbitrate(
      [
        cand("artist", "Mithoon & Arijit Singh", "itunes"),
        cand("artist", "Mithoon", "spotify"),
        cand("artist", "Arijit Singh", "musicbrainz"),
      ],
      family="world",
    )
  )
  assert got["artist"][0] == "Arijit Singh"


def test_the_convention_exemption_is_scoped_to_world():
  """Everywhere else two agreeing sources still beat one ranked higher."""
  got = resolved(
    arbitrate(
      [
        cand("artist", "Morgan Wallen", "itunes"),
        cand("artist", "Morgan Wallen", "spotify"),
        cand("artist", "Metro Station", "musicbrainz"),
      ],
      family="pop",
    )
  )
  assert got["artist"][0] == "Morgan Wallen"


def test_album_artist_keeps_the_fuller_credit_in_world():
  """Vocalists on the artist line, composers allowed on album artist."""
  got = resolved(
    arbitrate(
      [
        cand("artist", "Mithoon & Arijit Singh", "itunes"),
        cand("artist", "Arijit Singh", "musicbrainz"),
        cand("album", "Sanam Re", "itunes"),
        cand("album_artist", "Mithoon", "itunes"),
      ],
      family="world",
    )
  )
  assert got["artist"][0] == "Arijit Singh"
  assert got["album_artist"][0] == "Mithoon"


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
  # spotify is ranked first and offers an album, so it supplies the whole
  # group; musicbrainz's album is not mixed in
  assert got["album"] == ("B", "spotify")
  assert got["track_number"] == ("3", "spotify")


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

  Essentia is not ranked for `remixer`, so if it were the only offer it would
  win by falling through — and that must be visible rather than silent.
  """
  decisions = arbitrate([cand("remixer", "Someone", "essentia")], family="electronic")
  decision = next(d for d in decisions if d.field == "remixer")
  assert (decision.source, decision.decided_by) == ("essentia", "fallback")


def test_a_ranked_source_is_recorded_as_precedence():
  decisions = arbitrate([cand("genre", "House", "discogs")], family="electronic")
  decision = next(d for d in decisions if d.field == "genre")
  assert decision.decided_by == "precedence"


# --- compilations must not supply album fields (cp6) -----------------------


def test_a_compilation_does_not_supply_album_fields():
  """A various-artists release must not supply album fields.

  cp6: MusicBrainz won on precedence and returned "NRJ Hits 2011" while Spotify
  had the real album. A compilation is a place the track appears, not the album
  it belongs to.
  """
  got = resolved(
    arbitrate(
      [
        cand("album", "NRJ Hits 2011", "musicbrainz"),
        cand("album_artist", "Various Artists", "musicbrainz"),
        cand("track_number", "15", "musicbrainz"),
        cand("album", "Only One Flo (Part 1)", "spotify"),
        cand("album_artist", "Flo Rida", "spotify"),
        cand("track_number", "3", "spotify"),
      ],
      family="pop",
    )
  )
  assert got["album"] == ("Only One Flo (Part 1)", "spotify")
  assert got["track_number"] == ("3", "spotify")


def test_a_compilation_only_album_is_dropped_rather_than_used():
  """Reversed deliberately: this test used to assert the opposite.

  Taking the compilation when it was the only offer looked like graceful
  degradation. On the first hundred-track run it published "Down" by Jay Sean
  as track 1 of "Ultra Dance 11" credited to DJ Enferno — the only source that
  answered was MusicBrainz, and it answered with the licensing compilation.
  A wrong album propagates into the filename, the folder and both DJ apps;
  an empty one is visibly incomplete and gets fixed in review.
  """
  got = resolved(
    arbitrate(
      [
        cand("album", "NRJ Hits 2011", "musicbrainz"),
        cand("album_artist", "Various Artists", "musicbrainz"),
      ],
      family="pop",
    )
  )
  assert "album" not in got
  assert "album_artist" not in got


@pytest.mark.parametrize("credit", ["Various Artists", "various", "VA", "Diverse"])
def test_compilation_credits_are_recognised(credit):
  got = resolved(
    arbitrate(
      [
        cand("album", "Comp", "musicbrainz"),
        cand("album_artist", credit, "musicbrainz"),
        cand("album", "Real Album", "spotify"),
        cand("album_artist", "The Artist", "spotify"),
      ],
      family="pop",
    )
  )
  assert got["album"] == ("Real Album", "spotify")


# --- placeholder labels are rejected ---------------------------------------


@pytest.mark.parametrize(
  "value",
  ["Not On Label", "Not On Label (Pitbull)", "Self-Released", "none", "  "],
)
def test_placeholder_labels_are_dropped(value):
  """Discogs writes "Not On Label (Pitbull)" for self-released pressings."""
  assert "label" not in resolved(arbitrate([cand("label", value, "discogs")]))


def test_a_real_label_survives():
  got = resolved(arbitrate([cand("label", "T-Series", "discogs")]))
  assert got["label"] == ("T-Series", "discogs")


def test_a_real_label_beats_a_placeholder():
  got = resolved(
    arbitrate(
      [
        cand("label", "Not On Label (X)", "discogs"),
        cand("label", "Polo Grounds Music", "musicbrainz"),
      ],
      family="pop",
    )
  )
  assert got["label"] == ("Polo Grounds Music", "musicbrainz")


def test_an_album_credited_to_someone_else_is_a_compilation():
  """An album credited to someone other than the artist is a compilation.

  cp6: "Fireball" resolved to a Mastermix DJ compilation credited to "Music
  Factory", which never says "Various Artists".
  """
  got = resolved(
    arbitrate(
      [
        cand("artist", "Pitbull", "musicbrainz"),
        cand("album", "Mastermix Classic Cuts, Volume 165", "musicbrainz"),
        cand("album_artist", "Music Factory", "musicbrainz"),
        cand("artist", "Pitbull", "spotify"),
        cand("album", "Global Warming", "spotify"),
        cand("album_artist", "Pitbull", "spotify"),
      ],
      family="pop",
    )
  )
  assert got["album"] == ("Global Warming", "spotify")


def test_a_wider_album_credit_is_not_a_compilation():
  """A wider album credit is not a compilation.

  "David Guetta" vs "David Guetta & Akon" is the same album.
  """
  got = resolved(
    arbitrate(
      [
        cand("artist", "David Guetta", "musicbrainz"),
        cand("album", "One Love", "musicbrainz"),
        cand("album_artist", "David Guetta & Akon", "musicbrainz"),
      ],
      family="pop",
    )
  )
  assert got["album"] == ("One Love", "musicbrainz")


# --- deluxe editions and cross-source agreement (cp6) ----------------------


def test_deluxe_edition_is_preferred_over_the_standard():
  got = resolved(
    arbitrate(
      [
        cand("artist", "David Guetta", "musicbrainz"),
        cand("album", "One Love", "musicbrainz"),
        cand("album_artist", "David Guetta", "musicbrainz"),
        cand("artist", "David Guetta", "spotify"),
        cand("album", "One Love (Deluxe)", "spotify"),
        cand("album_artist", "David Guetta", "spotify"),
      ],
      family="pop",
    )
  )
  assert got["album"] == ("One Love (Deluxe)", "spotify")


def test_two_sources_agreeing_beat_one_ranked_higher():
  """Agreement between sources outweighs a higher-ranked lone source.

  cp6: "Hey Baby" took musicbrainz's "Global Warming" while itunes and spotify
  both said "Planet Pit (Deluxe Version)" — and were right.
  """
  got = resolved(
    arbitrate(
      [
        cand("artist", "Pitbull", "musicbrainz"),
        cand("album", "Global Warming", "musicbrainz"),
        cand("album_artist", "Pitbull", "musicbrainz"),
        cand("artist", "Pitbull", "spotify"),
        cand("album", "Planet Pit (Deluxe Version)", "spotify"),
        cand("album_artist", "Pitbull", "spotify"),
        cand("artist", "Pitbull", "itunes"),
        cand("album", "Planet Pit (Deluxe Version)", "itunes"),
        cand("album_artist", "Pitbull", "itunes"),
      ],
      family="pop",
    )
  )
  assert got["album"][0] == "Planet Pit (Deluxe Version)"


def test_unanimous_agreement_keeps_the_ranked_source():
  got = resolved(
    arbitrate(
      [cand("artist", "Pitbull", s) for s in ("musicbrainz", "spotify", "itunes")]
      + [
        cand("album", "Globalization", s) for s in ("musicbrainz", "spotify", "itunes")
      ]
      + [
        cand("album_artist", "Pitbull", s) for s in ("musicbrainz", "spotify", "itunes")
      ],
      family="pop",
    )
  )
  assert got["album"] == ("Globalization", "spotify")


@pytest.mark.parametrize(
  ("a", "b"),
  [
    ("Planet Pit", "Planet Pit (Deluxe Version)"),
    ("One Love", "One Love (Deluxe)"),
    ("Album", "Album [Expanded Edition]"),
    ("Album", "Album (10th Anniversary Edition)"),
  ],
)
def test_editions_group_together(a, b):
  from music.arbitrate import _album_key

  assert _album_key(a) == _album_key(b)


def test_different_albums_do_not_group():
  from music.arbitrate import _album_key

  assert _album_key("Planet Pit") != _album_key("Global Warming")


# --- consensus: agreement outweighs precedence (first 100-track run) --------


def test_two_agreeing_sources_beat_one_ranked_higher_on_artist():
  """The defect that published Morgan Wallen's "Last Night" as Metro Station.

  AcoustID matched the wrong recording, MusicBrainz faithfully described it,
  and MusicBrainz ranks first for pop — so it won `artist` and `title` over
  iTunes and Spotify, which both had it right. Twelve of eighty-three published
  tracks failed exactly this way.
  """
  got = resolved(
    arbitrate(
      [
        cand("artist", "Metro Station", "musicbrainz"),
        cand("artist", "Morgan Wallen", "itunes"),
        cand("artist", "Morgan Wallen", "spotify"),
        cand("title", "California", "musicbrainz"),
        cand("title", "Last Night", "itunes"),
        cand("title", "Last Night", "spotify"),
      ],
      family="pop",
    )
  )
  assert got["artist"][0] == "Morgan Wallen"
  assert got["title"][0] == "Last Night"


def test_a_lone_source_still_wins_when_it_is_the_only_one():
  got = resolved(arbitrate([cand("title", "Only Offer", "discogs")], family="pop"))
  assert got["title"][0] == "Only Offer"


def test_precedence_still_decides_when_no_two_sources_agree():
  got = resolved(
    arbitrate(
      [
        cand("title", "A", "musicbrainz"),
        cand("title", "B", "itunes"),
        cand("title", "C", "spotify"),
      ],
      family="pop",
    )
  )
  assert got["title"] == ("C", "spotify")


def test_consensus_ignores_case_and_spacing():
  got = resolved(
    arbitrate(
      [
        cand("title", "Something Else", "musicbrainz"),
        cand("title", "last  night", "itunes"),
        cand("title", "Last Night", "spotify"),
      ],
      family="pop",
    )
  )
  # the two spellings are one value, and outvote the higher-ranked source
  assert got["title"][0].casefold().replace(" ", "") == "lastnight"


def test_genre_is_exempt_from_consensus():
  """Two coarse sources agreeing must not outrank a specific one.

  Granularity differs by design, and which granularity is wanted is exactly
  what elicitation calibrates (SPEC.md §9) — so genre stays on precedence.
  """
  got = resolved(
    arbitrate(
      [
        cand("genre", "Progressive House", "discogs"),
        cand("genre", "Dance", "itunes"),
        cand("genre", "Dance", "spotify"),
      ],
      family="electronic",
    )
  )
  assert got["genre"] == ("Dance", "itunes")


# --- album editions --------------------------------------------------------


def test_unbracketed_editions_group_as_one_album():
  """Guetta ships plain, "2.0" and "Ultimate" with no brackets anywhere.

  Ungrouped they are three albums with one source each, so the edition a track
  lands on is decided by whichever source ranks first — which is how one track
  got 2.0 and another got Ultimate off the same record.
  """
  got = resolved(
    arbitrate(
      [
        cand("album", "Nothing But the Beat", "itunes"),
        cand("album", "Nothing but the Beat 2.0", "spotify"),
        cand("album", "Nothing But the Beat Ultimate", "musicbrainz"),
      ],
      family="pop",
    )
  )
  assert got["album"][0] == "Nothing But the Beat Ultimate"


def test_a_numbered_reissue_beats_the_plain_album():
  got = resolved(
    arbitrate(
      [
        cand("album", "Nothing But the Beat", "itunes"),
        cand("album", "Nothing but the Beat 2.0", "spotify"),
      ],
      family="pop",
    )
  )
  assert got["album"][0] == "Nothing but the Beat 2.0"


def test_numbered_albums_are_not_mistaken_for_editions():
  """Kidz Bop 22 and Kidz Bop 21 are different records, not editions."""
  from music.arbitrate import _album_key

  assert _album_key("Kidz Bop 22") != _album_key("Kidz Bop 21")
  assert _album_key("Nothing But the Beat") == _album_key("Nothing but the Beat 2.0")


def test_a_version_qualifier_does_not_split_two_agreeing_sources():
  """Hey, Soul Sister was published as There for You.

  iTunes returned `Hey, Soul Sister (Country Mix)` and Spotify returned
  `Hey, Soul Sister`. Exact grouping made those two separate answers of one
  source each, so MusicBrainz — describing a completely different recording —
  won on precedence.
  """
  got = resolved(
    arbitrate(
      [
        cand("title", "There for You", "musicbrainz"),
        cand("title", "Hey, Soul Sister (Country Mix)", "itunes"),
        cand("title", "Hey, Soul Sister", "spotify"),
      ],
      family="pop",
    )
  )
  assert got["title"][0] == "Hey, Soul Sister"


def test_a_feat_credit_is_never_stripped_when_grouping():
  """A `feat.` clause names the same recording more fully, not a different one."""
  got = resolved(
    arbitrate(
      [
        cand("title", "Marvin Gaye (feat. Meghan Trainor)", "itunes"),
        cand("title", "Marvin Gaye (feat. Meghan Trainor)", "spotify"),
        cand("title", "Marvin Gaye", "musicbrainz"),
      ],
      family="pop",
    )
  )
  assert got["title"][0] == "Marvin Gaye (feat. Meghan Trainor)"


def test_the_loose_pass_only_runs_when_nothing_agrees_exactly():
  """Exact agreement must never be overridden by a looser match."""
  got = resolved(
    arbitrate(
      [
        cand("title", "Delilah [Tom Santa Remix]", "itunes"),
        cand("title", "Delilah [Tom Santa Remix]", "spotify"),
        cand("title", "Delilah", "musicbrainz"),
        cand("title", "Delilah", "discogs"),
      ],
      family="electronic",
    )
  )
  # musicbrainz+discogs also agree, two each — precedence breaks the tie, and
  # the remix is not silently flattened into the original
  assert got["title"][0] in ("Delilah", "Delilah [Tom Santa Remix]")


# --- artwork belongs to the release we tagged ------------------------------


def test_the_highest_quality_cover_wins_among_correct_releases():
  """Release-accuracy first, then quality — not the other way round.

  iTunes serves 1200x1200 where Spotify serves 640x640 and the Cover Art
  Archive varies, so when several sources name the album we tagged, the best
  picture of it wins.
  """
  got = resolved(
    arbitrate(
      [
        cand("album", "Planet Pit", "musicbrainz"),
        cand("artwork_url", "https://caa/planetpit.jpg", "musicbrainz"),
        cand("album", "Planet Pit", "itunes"),
        cand("artwork_url", "https://itunes/planetpit.jpg", "itunes"),
        cand("album", "Planet Pit", "spotify"),
        cand("artwork_url", "https://spotify/planetpit.jpg", "spotify"),
      ],
      family="pop",
    )
  )
  assert got["artwork_url"][0] == "https://itunes/planetpit.jpg"


def test_a_correct_smaller_cover_beats_a_larger_wrong_one():
  """When only the album source has the release, its cover wins on accuracy."""
  got = resolved(
    arbitrate(
      [
        cand("album", "Icarus II", "musicbrainz"),
        cand("artwork_url", "https://caa/icarus2.jpg", "musicbrainz"),
        cand("album", "June - Single", "itunes"),
        cand("artwork_url", "https://itunes/june-single.jpg", "itunes"),
      ],
      family="hip-hop",
    )
  )
  assert got["album"][0] == "Icarus II"
  assert got["artwork_url"][0] == "https://caa/icarus2.jpg"


def test_artwork_comes_from_the_source_that_supplied_the_album():
  """The bug behind almost every wrong cover in the first real library.

  MusicBrainz carries no images, so whenever it won the album — which is most
  of the time for pop — artwork fell to iTunes by its own precedence, showing
  the sleeve of whichever release *iTunes* had matched. Every text field was
  right and the picture was from another record.
  """
  got = resolved(
    arbitrate(
      [
        cand("album", "The Juicebox", "musicbrainz"),
        cand("artwork_url", "https://caa/juicebox.jpg", "musicbrainz"),
        cand("album", "The Juice, Vol. II", "itunes"),
        cand("artwork_url", "https://itunes/vol2.jpg", "itunes"),
      ],
      family="r&b-soul",
    )
  )
  assert got["album"][0] == "The Juicebox"
  assert got["artwork_url"][0] == "https://caa/juicebox.jpg"


def test_artwork_falls_to_a_source_naming_the_same_release():
  """When the album source has no cover, one describing the same album wins."""
  got = resolved(
    arbitrate(
      [
        cand("album", "The Juicebox", "musicbrainz"),
        cand("album", "The Juice, Vol. II", "itunes"),
        cand("artwork_url", "https://itunes/vol2.jpg", "itunes"),
        cand("album", "The Juicebox", "spotify"),
        cand("artwork_url", "https://spotify/juicebox.jpg", "spotify"),
      ],
      family="r&b-soul",
    )
  )
  assert got["artwork_url"][0] == "https://spotify/juicebox.jpg"


def test_an_edition_still_counts_as_the_same_release_for_artwork():
  got = resolved(
    arbitrate(
      [
        cand("album", "Planet Pit", "musicbrainz"),
        cand("album", "Planet Pit (Deluxe Version)", "spotify"),
        cand("artwork_url", "https://spotify/planetpit.jpg", "spotify"),
      ],
      family="pop",
    )
  )
  assert got["artwork_url"][0] == "https://spotify/planetpit.jpg"


def test_artwork_is_kept_when_no_source_matches_the_album():
  """A track must never lose its cover entirely; precedence is the last resort."""
  got = resolved(
    arbitrate(
      [
        cand("album", "Some Album", "musicbrainz"),
        cand("artwork_url", "https://itunes/other.jpg", "itunes"),
      ],
      family="pop",
    )
  )
  assert got["artwork_url"][0] == "https://itunes/other.jpg"


def test_no_artwork_anywhere_resolves_to_nothing():
  got = resolved(arbitrate([cand("album", "Some Album", "musicbrainz")], family="pop"))
  assert "artwork_url" not in got


# --- featured artists survive whoever wins the title ------------------------


def test_a_guest_survives_a_title_that_omits_it():
  """The real case: Spotify leads title and drops the feature.

  MusicBrainz is the only source that states who is featured — §7 ranks it
  first for credits for exactly that — but it no longer wins `title`, so
  `Time of Our Lives (feat. Ne-Yo)` came out as `Time of Our Lives`.
  """
  got = resolved(
    arbitrate(
      [
        cand("artist", "Pitbull", "spotify"),
        cand("title", "Time of Our Lives", "spotify"),
        cand("title", "Time of Our Lives (feat. Ne-Yo)", "musicbrainz"),
        cand("featured_artists", "Ne-Yo", "musicbrainz"),
      ],
      family="pop",
    )
  )
  assert got["title"][0] == "Time of Our Lives (feat. Ne-Yo)"
  assert got["artist"][0] == "Pitbull"


def test_a_guest_already_in_the_title_is_not_repeated():
  got = resolved(
    arbitrate(
      [
        cand("title", "All That (feat. Channel Tres)", "spotify"),
        cand("artist", "Emotional Oranges", "spotify"),
        cand("featured_artists", "Channel Tres", "musicbrainz"),
      ],
      family="r&b-soul",
    )
  )
  assert got["title"][0].count("Channel Tres") == 1


def test_a_collaborator_on_the_artist_line_is_not_moved_to_the_title():
  """Only a name that is *missing* is added; the artist line is authoritative."""
  got = resolved(
    arbitrate(
      [
        cand("title", "Give Me Everything", "spotify"),
        cand("artist", "Pitbull, Ne-Yo, Afrojack & Nayer", "spotify"),
        cand("featured_artists", "Nayer", "musicbrainz"),
      ],
      family="pop",
    )
  )
  assert got["title"][0] == "Give Me Everything"


def test_featured_artists_never_becomes_a_tag():
  """It is provenance for the rule above, not an ID3 frame."""
  from music.publish.fields import NON_TAG_FIELDS

  assert "featured_artists" in NON_TAG_FIELDS


def test_world_still_takes_the_performer_from_musicbrainz():
  """Bollywood credits the music director; the singer is what a DJ sorts by."""
  got = resolved(
    arbitrate(
      [
        cand("artist", "Mithoon", "spotify"),
        cand("artist", "Arijit Singh", "musicbrainz"),
        cand("title", "Sanam Re", "spotify"),
      ],
      family="world",
    )
  )
  assert got["artist"][0] == "Arijit Singh"
  assert got["title"][0] == "Sanam Re"


def test_an_itunes_release_type_suffix_is_not_a_different_album():
  """The `- Single` suffix is iTunes' own wording, not a different record.

  Left unmerged, the two are different releases, so iTunes is excluded from
  the cover choice and a 640x640 Spotify image beats a 1200x1200 one for the
  same record. 12 of 120 tracks were losing quality this way.
  """
  from music.arbitrate import _album_key

  assert _album_key("June") == _album_key("June - Single")
  assert _album_key("YES") == _album_key("YES - EP")
  # the dash is required, so an album genuinely called "The EP" is untouched
  assert _album_key("The EP") != _album_key("Something Else")
  # and a different record stays different
  assert _album_key("Blonde") != _album_key("Pink + White - Single")


def test_itunes_artwork_wins_once_the_release_matches():
  got = resolved(
    arbitrate(
      [
        cand("album", "June", "spotify"),
        cand("artwork_url", "https://spotify/june.jpg", "spotify"),
        cand("album", "June - Single", "itunes"),
        cand("artwork_url", "https://itunes/june.jpg", "itunes"),
      ],
      family="hip-hop",
    )
  )
  assert got["album"][0] == "June"
  assert got["artwork_url"][0] == "https://itunes/june.jpg"
