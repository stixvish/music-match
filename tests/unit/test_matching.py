"""Tests for normalisation, candidate scoring and cross-source merging."""

import pytest

from music_match.config import loader
from music_match.matching import matcher
from music_match.matching import normalize as norm
from music_match.matching import score as scoring
from music_match.sources.base import SourceQuery
from music_match.sources.base import SourceResult
from music_match.tagging.fields import TrackTags

# ------------------------------------------------------------ normalize


@pytest.mark.parametrize("raw,expected", [
    ("Around the World", "around the world"),
    ("Around The World", "around the world"),
    ("Röyksopp", "royksopp"),
    ("Mood (feat. iann dior)", "mood"),
    ("Left and Right (feat. Jung Kook)", "left and right"),
])
def test_normalize_folds_incidental_differences(raw: str,
                                                expected: str) -> None:
  """Case, accents and featured artists are not real differences."""
  assert norm.normalize(raw) == expected


def test_normalize_survives_a_mangled_filename() -> None:
  """yt-dlp names carry underscores and upload noise."""
  mangled = "Daft_Punk_-_Around_the_World_Official_Video"
  assert norm.normalize(mangled) == "daft punk around the world"


def test_feature_stripping_needs_word_boundaries() -> None:
  """"ft" inside a word is not a featured-artist marker.

  Without the boundary this turns "Daft Punk - Around the World" into
  "Da", which then matches nothing.
  """
  assert norm.normalize("Daft Punk") == "daft punk"


def test_variants_are_not_normalised_away() -> None:
  """A remix is a different recording and must stay distinguishable."""
  assert norm.normalize("Strobe") != norm.normalize("Strobe (Club Edit)")


def test_with_is_not_treated_as_a_feature_marker() -> None:
  """Stripping a trailing "with ..." would gut ordinary titles."""
  assert norm.normalize("Sing With Me") == "sing with me"


@pytest.mark.parametrize("album,expected", [
    ("Homework", "homework"),
    ("Homework (25th Anniversary Edition)", "homework"),
    ("For Lack of A Better Name (The Extended Mixes)",
     "for lack of a better name"),
    ("Settle (Special Edition)", "settle"),
    ("Strobe - Single", "strobe"),
])
def test_release_key_ignores_editions(album: str, expected: str) -> None:
  """Sources that agree on a record must not split the vote over its edition."""
  assert norm.release_key(album) == expected


@pytest.mark.parametrize("album", [
    "Sgt. Pepper (Reprise)",
    "Blue Train (Alternate Take)",
])
def test_release_key_keeps_a_real_title(album: str) -> None:
  """A bracket that is part of the title is not an edition marker.

  Matched as substrings, short markers hide inside ordinary words — "ep"
  sits inside "Reprise" — and would strip a real part of the title.
  """
  assert norm.release_key(album) == norm.normalize(album)


# ---------------------------------------------------------------- score


def result(title: str | None = "Strobe",
           artist: str | None = "deadmau5",
           album: str | None = None,
           duration: float | None = None) -> SourceResult:
  """Builds a candidate.

  Args:
    title: The candidate's title.
    artist: The candidate's artist.
    album: The candidate's album.
    duration: The candidate's duration in seconds.

  Returns:
    The constructed candidate.
  """
  return SourceResult(source="test",
                      source_id="1",
                      tags=TrackTags(title=title, artist=artist, album=album),
                      duration_seconds=duration)


QUERY = SourceQuery(title="Strobe", artist="deadmau5", duration_seconds=636.0)


def test_an_exact_match_scores_top() -> None:
  """Everything agreeing is the best a candidate can do."""
  score = scoring.score_candidate(QUERY, result(duration=636.0), 636.0)
  assert score.total == pytest.approx(1.0)


def test_a_different_song_scores_low() -> None:
  """A wrong title is not rescued by a right artist."""
  score = scoring.score_candidate(QUERY, result(title="Ghosts n Stuff"))
  assert score.total < 0.6


def test_duration_separates_a_live_take() -> None:
  """A live cut runs minutes longer; nothing else distinguishes it."""
  studio = scoring.score_candidate(QUERY, result(duration=636.0), 636.0)
  live = scoring.score_candidate(QUERY, result(duration=780.0), 780.0)
  assert studio.total > live.total


def test_missing_duration_does_not_cap_the_score() -> None:
  """A file with no known duration is judged on what is known.

  Scoring the missing signal as zero would push every candidate below
  every threshold.
  """
  query = SourceQuery(title="Strobe", artist="deadmau5")
  assert scoring.score_candidate(query, result()).total == pytest.approx(1.0)


def test_soundtracks_and_compilations_are_penalised() -> None:
  """These are what a text search surfaces and rarely what to tag from."""
  plain = scoring.score_candidate(QUERY,
                                  result(album="For Lack of a Better Name"))
  hits = scoring.score_candidate(QUERY, result(album="Greatest Hits"))
  assert hits.total < plain.total
  assert hits.reasons


def test_a_live_track_is_not_penalised_for_a_live_album() -> None:
  """If the track is a live recording, a live album is the right release."""
  query = SourceQuery(title="Strobe (Live)", artist="deadmau5")
  score = scoring.score_candidate(
      query, result(title="Strobe (Live)", album="Live at Earls Court"))
  assert not score.reasons


def test_a_variant_marker_costs_a_candidate() -> None:
  """"Strobe" and "Strobe (Club Edit)" are different recordings."""
  score = scoring.score_candidate(QUERY, result(title="Strobe (Club Edit)"))
  assert score.penalty > 0


def test_matching_duration_overrides_the_variant_penalty() -> None:
  """A file that runs exactly as long as the edit *is* the edit.

  The file simply was not labelled. Duration is physical evidence about
  the recording; a title marker is a labelling convention.
  """
  query = SourceQuery(title="Get Lucky",
                      artist="Daft Punk",
                      duration_seconds=249.0)
  score = scoring.score_candidate(
      query, result(title="Get Lucky (Radio Edit)", artist="Daft Punk"), 248.0)
  assert score.penalty == 0
  # The title similarity is genuinely lower, so this does not reach 1.0 —
  # what matters is that it comfortably clears the candidate gate rather
  # than being dropped as it was before.
  assert score.total > matcher.MINIMUM_CANDIDATE_SCORE + 0.2


def test_similarity_survives_reordered_names() -> None:
  """Sources order collaborating artists differently."""
  assert scoring.similarity("Vishal-Shekhar, Benny Dayal",
                            "Benny Dayal, Vishal-Shekhar") > 0.7


def test_similarity_of_nothing_is_zero() -> None:
  """An absent value matches nothing rather than everything."""
  assert scoring.similarity(None, "Strobe") == 0.0


# --------------------------------------------------------------- merging


def candidate(name: str, **kwargs: object) -> matcher.SourceCandidate:
  """Builds a scored candidate from one source.

  Args:
    name: The source name.
    **kwargs: Passed to `result`.

  Returns:
    The scored candidate.
  """
  built = SourceResult(source=name, source_id="1", tags=TrackTags(**kwargs))
  return matcher.SourceCandidate(result=built,
                                 score=scoring.score_candidate(QUERY, built))


def precedence_with(order: list[str]) -> loader.PrecedenceConfig:
  """Builds a precedence config with one default ordering.

  Args:
    order: The source order.

  Returns:
    The configuration.
  """
  return loader.PrecedenceConfig(
      genres={"default": loader.GenrePrecedence(order=tuple(order), fields={})})


def test_precedence_decides_an_uncontested_field() -> None:
  """With no disagreement, the configured order wins."""
  candidates = {
      "discogs":
          candidate("discogs", title="Strobe", genre="Progressive House"),
      "itunes":
          candidate("itunes", title="Strobe", genre="Dance"),
  }
  resolver = matcher.OrderResolver(precedence_with(["discogs", "itunes"]))
  tags, sources, _ = matcher.merge(QUERY, candidates, resolver)
  assert tags.genre == "Progressive House"
  assert sources["genre"] == "discogs"


def test_agreeing_sources_outvote_the_preferred_one() -> None:
  """The soundtrack case: nothing but disagreement reveals the wrong release.

  Title, artist and duration all match for both, so per-candidate scoring
  cannot separate them. Two sources naming the same album can.
  """
  candidates = {
      "discogs":
          candidate("discogs", title="Thunderstruck", album="Iron Man 2"),
      "spotify":
          candidate("spotify", title="Thunderstruck", album="The Razors Edge"),
      "itunes":
          candidate("itunes", title="Thunderstruck", album="The Razors Edge"),
  }
  resolver = matcher.OrderResolver(
      precedence_with(["discogs", "spotify", "itunes"]))
  tags, sources, notes = matcher.merge(QUERY, candidates, resolver)
  assert tags.album == "The Razors Edge"
  assert sources["album"] != "discogs"
  assert notes


def test_a_plurality_is_enough_to_outvote() -> None:
  """With four sources a 2-1-1 split is only a 0.5 share.

  Requiring a strict majority there would keep one source's answer over
  the two that agree.
  """
  candidates = {
      "discogs": candidate("discogs", title="X", album="Single"),
      "musicbrainz": candidate("musicbrainz", title="X", album="Compilation"),
      "spotify": candidate("spotify", title="X", album="Real Album"),
      "itunes": candidate("itunes", title="X", album="Real Album"),
  }
  resolver = matcher.OrderResolver(
      precedence_with(["discogs", "musicbrainz", "spotify", "itunes"]))
  tags, _, _ = matcher.merge(QUERY, candidates, resolver)
  assert tags.album == "Real Album"


def test_editions_of_one_album_count_as_agreement() -> None:
  """Sources agreeing on a record must not split over its edition."""
  candidates = {
      "discogs":
          candidate("discogs", title="X", album="Some Single"),
      "spotify":
          candidate("spotify", title="X", album="The Album"),
      "itunes":
          candidate("itunes", title="X", album="The Album (Deluxe Edition)"),
  }
  resolver = matcher.OrderResolver(
      precedence_with(["discogs", "spotify", "itunes"]))
  tags, _, _ = matcher.merge(QUERY, candidates, resolver)
  assert tags.album == "The Album"


def test_titles_are_not_voted_on() -> None:
  """Sources rarely dispute a title; precedence is enough for it."""
  candidates = {
      "discogs": candidate("discogs", title="Strobe"),
      "spotify": candidate("spotify", title="Strobe (Radio Edit)"),
      "itunes": candidate("itunes", title="Strobe (Radio Edit)"),
  }
  resolver = matcher.OrderResolver(
      precedence_with(["discogs", "spotify", "itunes"]))
  tags, _, _ = matcher.merge(QUERY, candidates, resolver)
  assert tags.title == "Strobe"


# ------------------------------------------------------------ confidence


def test_disagreement_lowers_confidence() -> None:
  """Four sources naming four albums is not a confident match."""
  agreeing = {
      "spotify": candidate("spotify", title="X", album="A"),
      "itunes": candidate("itunes", title="X", album="A"),
  }
  arguing = {
      "spotify": candidate("spotify", title="X", album="A"),
      "itunes": candidate("itunes", title="X", album="B"),
  }
  assert (matcher.confidence_for(agreeing, matcher.core_agreement(agreeing))
          > matcher.confidence_for(arguing, matcher.core_agreement(arguing)))


def test_one_source_alone_is_weaker_than_several() -> None:
  """Breadth is evidence: one source scoring well proves less."""
  alone = {"spotify": candidate("spotify", title="Strobe")}
  several = {
      "spotify": candidate("spotify", title="Strobe"),
      "itunes": candidate("itunes", title="Strobe"),
      "discogs": candidate("discogs", title="Strobe"),
  }
  assert (matcher.confidence_for(several, matcher.core_agreement(several))
          > matcher.confidence_for(alone, matcher.core_agreement(alone)))


@pytest.mark.parametrize("confidence,expected", [
    (0.95, matcher.MatchStatus.MATCHED),
    (0.85, matcher.MatchStatus.MATCHED),
    (0.60, matcher.MatchStatus.REVIEW),
    (0.10, matcher.MatchStatus.NO_MATCH),
])
def test_confidence_maps_to_a_status(confidence: float, expected: str) -> None:
  """The three outcomes the pipeline acts on."""
  assert matcher.status_for(confidence, 0.85, 0.45) == expected


@pytest.mark.parametrize("stem,artist,title", [
    ("Tiesto - All Nighter (Extended Mix)", "Tiesto",
     "All Nighter (Extended Mix)"),
    ("Loud Luxury, ZOHARA - COLORADO (Extended)", "Loud Luxury, ZOHARA",
     "COLORADO (Extended)"),
    ("Daft_Punk_-_Around_the_World", "Daft Punk", "Around the World"),
    ("Untitled Track", None, "Untitled Track"),
])
def test_filename_yields_a_query(stem: str, artist: str | None,
                                 title: str) -> None:
  """A file with no title tag is not a lost cause.

  WAV's tagging support is an afterthought, so the beatport files carry
  no title at all — but their names read "Artist - Title", which is
  enough to search on.
  """
  assert norm.split_filename(stem) == (artist, title)


def test_filename_split_ignores_a_leading_separator() -> None:
  """A name that starts with the separator has no artist half."""
  assert norm.split_filename(" - Title")[0] is None
