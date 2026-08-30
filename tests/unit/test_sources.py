"""Tests for the metadata source adapters.

Every adapter is exercised against recorded response shapes rather than
the live API — the fixtures below are trimmed copies of what each service
actually returned for a real track. A unit test must never depend on a
third party being up, or on their data not changing.
"""

from typing import Any

import pytest

from music_match.config import env
from music_match.sources import base
from music_match.sources import discogs
from music_match.sources import http
from music_match.sources import itunes
from music_match.sources import musicbrainz
from music_match.sources import spotify


class FakeClient:
  """Returns canned bodies instead of making requests."""

  def __init__(self, bodies: list[Any] | Any) -> None:
    """Records what to return.

    Args:
      bodies: A body to return for every call, or a list returned in
        order.
    """
    self._bodies = bodies if isinstance(bodies, list) else [bodies]
    self.calls: list[tuple[str, Any]] = []
    self.cache = None

  def _next(self, url: str, params: Any) -> Any:
    """Records a call and returns the next canned body.

    Args:
      url: The requested URL.
      params: The query parameters.

    Returns:
      The next body, repeating the last one once exhausted.
    """
    self.calls.append((url, params))
    index = min(len(self.calls) - 1, len(self._bodies) - 1)
    return self._bodies[index]

  def get_json(self,
               url: str,
               params: Any = None,
               headers: Any = None,
               **_: Any) -> Any:
    """Returns the next canned body for a GET.

    Args:
      url: The requested URL.
      params: The query parameters.
      headers: Ignored.

    Returns:
      The canned body.
    """
    del headers
    return self._next(url, params)

  def post_json(self, url: str, data: Any = None, headers: Any = None) -> Any:
    """Returns the next canned body for a POST.

    Args:
      url: The requested URL.
      data: The form body.
      headers: Ignored.

    Returns:
      The canned body.
    """
    del headers
    return self._next(url, data)


QUERY = base.SourceQuery(title="Around the World", artist="Daft Punk")

# ---------------------------------------------------------------- iTunes

ITUNES_BODY = {
    "resultCount":
        1,
    "results": [{
        "trackId": 696886431,
        "trackName": "Around the World",
        "artistName": "Daft Punk",
        "collectionName": "Homework",
        "primaryGenreName": "Dance",
        "releaseDate": "1997-01-20T08:00:00Z",
        "discNumber": 1,
        "discCount": 1,
        "trackNumber": 7,
        "trackCount": 16,
        "artworkUrl100": "https://example.com/a/100x100bb.jpg",
    }]
}


def test_itunes_maps_every_field_it_has() -> None:
  """An iTunes result becomes TrackTags with the fields it carries."""
  source = itunes.ITunesSource(FakeClient(ITUNES_BODY))
  result = source.search(QUERY)[0]
  tags = result.tags
  assert tags.title == "Around the World"
  assert tags.album == "Homework"
  assert tags.genre == "Dance"
  assert tags.release_date == "1997-01-20"
  assert tags.year == 1997
  assert (tags.track_number, tags.track_total) == (7, 16)
  assert (tags.disc_number, tags.disc_total) == (1, 1)


def test_itunes_upgrades_artwork_to_the_embedded_size() -> None:
  """The 100x100 URL is rewritten to the 640x640 this tool embeds."""
  source = itunes.ITunesSource(FakeClient(ITUNES_BODY))
  result = source.search(QUERY)[0]
  assert result.art_url == "https://example.com/a/640x640bb.jpg"
  assert result.art_size == 640


def test_itunes_needs_no_credentials() -> None:
  """iTunes is always available."""
  assert itunes.ITunesSource(FakeClient({})).is_available()


@pytest.mark.parametrize("body", [{}, {"results": None}, {"results": []}])
def test_itunes_handles_an_empty_answer(body: Any) -> None:
  """No results is an empty list, not an exception."""
  assert not itunes.ITunesSource(FakeClient(body)).search(QUERY)


# --------------------------------------------------------------- Spotify

SPOTIFY_TOKEN = {"access_token": "token-value", "expires_in": 3600}
SPOTIFY_BODY = {
    "tracks": {
        "items": [{
            "id": "1pKYYY0dkg23sQQXi0Q5zN",
            "name": "Around the World",
            "artists": [{
                "name": "Daft Punk"
            }],
            "disc_number": 1,
            "track_number": 7,
            "external_ids": {
                "isrc": "GBDUW0600009"
            },
            "album": {
                "name":
                    "Homework",
                "artists": [{
                    "name": "Daft Punk"
                }],
                "release_date":
                    "1997-01-17",
                "total_tracks":
                    16,
                "images": [
                    {
                        "width": 64,
                        "url": "https://example.com/64.jpg"
                    },
                    {
                        "width": 640,
                        "url": "https://example.com/640.jpg"
                    },
                ],
            },
        }]
    }
}


def test_spotify_carries_the_isrc(monkeypatch: pytest.MonkeyPatch) -> None:
  """Spotify returns an ISRC in the search response itself.

  This is the reason it leads the ISRC ordering: every other source here
  either lacks ISRCs or needs a second request for them.
  """
  monkeypatch.setenv(spotify.CLIENT_ID_VAR, "id")
  monkeypatch.setenv(spotify.CLIENT_SECRET_VAR, "secret")
  source = spotify.SpotifySource(FakeClient([SPOTIFY_TOKEN, SPOTIFY_BODY]))
  tags = source.search(QUERY)[0].tags
  assert tags.isrc == "GBDUW0600009"
  assert tags.release_date == "1997-01-17"
  assert tags.year == 1997


def test_spotify_picks_the_largest_image(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """The biggest cover offered is the one worth embedding."""
  monkeypatch.setenv(spotify.CLIENT_ID_VAR, "id")
  monkeypatch.setenv(spotify.CLIENT_SECRET_VAR, "secret")
  source = spotify.SpotifySource(FakeClient([SPOTIFY_TOKEN, SPOTIFY_BODY]))
  result = source.search(QUERY)[0]
  assert result.art_url == "https://example.com/640.jpg"
  assert result.art_size == 640


def test_spotify_reuses_its_token(monkeypatch: pytest.MonkeyPatch) -> None:
  """A second search does not exchange credentials again.

  The token lasts an hour; re-fetching it per query would be a wasted
  round trip on every single track.
  """
  monkeypatch.setenv(spotify.CLIENT_ID_VAR, "id")
  monkeypatch.setenv(spotify.CLIENT_SECRET_VAR, "secret")
  client = FakeClient([SPOTIFY_TOKEN, SPOTIFY_BODY, SPOTIFY_BODY])
  source = spotify.SpotifySource(client)
  source.search(QUERY)
  source.search(QUERY)
  token_calls = [call for call in client.calls if call[0] == spotify.TOKEN_URL]
  assert len(token_calls) == 1


def test_spotify_is_unavailable_without_credentials(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """Missing keys are reported rather than discovered mid-run."""
  monkeypatch.delenv(spotify.CLIENT_ID_VAR, raising=False)
  monkeypatch.delenv(spotify.CLIENT_SECRET_VAR, raising=False)
  assert not spotify.SpotifySource(FakeClient({})).is_available()


def test_spotify_query_uses_field_filters() -> None:
  """A known artist is sent as a filter, not as free text."""
  expression = spotify.search_expression(QUERY)
  assert 'track:"Around the World"' in expression
  assert 'artist:"Daft Punk"' in expression


# ----------------------------------------------------------- MusicBrainz

MB_BODY = {
    "recordings": [{
        "id":
            "de0c999d",
        "title":
            "Around the World",
        "artist-credit": [{
            "name": "Daft Punk",
            "joinphrase": ""
        }],
        "first-release-date":
            "1997-01-17",
        "releases": [{
            "id": "170d8783",
            "title": "Homework",
            "date": "1997-01-20",
            "track-count": 16,
        }],
    }]
}
MB_ISRCS = {"isrcs": ["GBDUW0600009"]}


def test_musicbrainz_joins_artist_credits() -> None:
  """Credit join phrases are preserved, so "A feat. B" survives."""
  body = {
      "recordings": [{
          "id":
              "x",
          "title":
              "T",
          "artist-credit": [
              {
                  "name": "A",
                  "joinphrase": " feat. "
              },
              {
                  "name": "B",
                  "joinphrase": ""
              },
          ],
      }]
  }
  source = musicbrainz.MusicBrainzSource(FakeClient(body), fetch_isrcs=False)
  assert source.search(QUERY)[0].tags.artist == "A feat. B"


def test_musicbrainz_reads_release_detail() -> None:
  """Album, date and track total come off the first release."""
  source = musicbrainz.MusicBrainzSource(FakeClient(MB_BODY), fetch_isrcs=False)
  tags = source.search(QUERY)[0].tags
  assert tags.album == "Homework"
  assert tags.release_date == "1997-01-20"
  assert tags.track_total == 16


def test_musicbrainz_isrc_costs_a_second_request() -> None:
  """The search response has no ISRC, so a lookup is made for it."""
  client = FakeClient([MB_BODY, MB_ISRCS])
  source = musicbrainz.MusicBrainzSource(client, fetch_isrcs=True)
  assert source.search(QUERY)[0].tags.isrc == "GBDUW0600009"
  assert len(client.calls) == 2


def test_musicbrainz_can_skip_the_isrc_lookup() -> None:
  """With lookups off, only the search request is made."""
  client = FakeClient(MB_BODY)
  source = musicbrainz.MusicBrainzSource(client, fetch_isrcs=False)
  assert source.search(QUERY)[0].tags.isrc is None
  assert len(client.calls) == 1


def test_musicbrainz_query_is_escaped() -> None:
  """A quote in a title cannot break out of the Lucene query."""
  query = base.SourceQuery(title='He said "hi"', artist="A")
  assert '\\"' in musicbrainz.lucene_query(query)


def test_musicbrainz_rate_limit_is_at_least_one_second() -> None:
  """The service enforces one request a second; respect it."""
  assert musicbrainz.MIN_INTERVAL_SECONDS >= 1.0


# --------------------------------------------------------------- Discogs

DISCOGS_SEARCH = {
    "results": [{
        "id": 2118837,
        "title": "deadmau5 - Strobe",
        "year": "2010",
        "genre": ["Electronic"],
        "style": ["Progressive House"],
        "label": ["mau5trap"],
        "catno": "MAU5015",
        "country": "UK",
        "cover_image": "https://example.com/cover.jpg",
    }]
}
DISCOGS_RELEASE = {
    "extraartists": [{
        "name": "Joel Zimmerman",
        "role": "Written-By"
    }],
    "tracklist": [
        {
            "position": "A",
            "title": "Strobe (Original)",
            "extraartists": []
        },
        {
            "position":
                "B1",
            "title":
                "Strobe (DJ Marky & S.P.Y Remix)",
            "extraartists": [
                {
                    "name": "DJ Marky",
                    "role": "Remix, Producer"
                },
                {
                    "name": "S.P.Y.",
                    "role": "Remix"
                },
            ],
        },
    ],
}


def discogs_source(monkeypatch: pytest.MonkeyPatch,
                   bodies: list[Any]) -> discogs.DiscogsSource:
  """Builds a Discogs source with a token set and canned responses.

  Args:
    monkeypatch: pytest's patching fixture.
    bodies: Canned response bodies, in call order.

  Returns:
    The configured source.
  """
  monkeypatch.setenv(discogs.TOKEN_VAR, "token")
  return discogs.DiscogsSource(FakeClient(bodies))


def test_discogs_finds_a_remixer_on_the_tracklist(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """Remix credits sit on the track, not the release.

  A record holding four remixes credits each remixer against their own
  track. Reading only release-level credits finds no remixer at all,
  which would make Discogs look useless at the one field it leads on.
  """
  source = discogs_source(monkeypatch, [DISCOGS_SEARCH, DISCOGS_RELEASE])
  query = base.SourceQuery(title="Strobe (DJ Marky & S.P.Y Remix)",
                           artist="deadmau5")
  tags = source.search(query)[0].tags
  assert tags.remixer == "DJ Marky, S.P.Y."
  assert tags.mix_name == "DJ Marky & S.P.Y Remix"


def test_discogs_does_not_invent_a_remixer(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """The original track has no remix credit, so none is reported."""
  source = discogs_source(monkeypatch, [DISCOGS_SEARCH, DISCOGS_RELEASE])
  query = base.SourceQuery(title="Strobe (Original)", artist="deadmau5")
  assert source.search(query)[0].tags.remixer is None


def test_discogs_takes_release_level_credits(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """A writer credited for the whole release applies to its tracks."""
  source = discogs_source(monkeypatch, [DISCOGS_SEARCH, DISCOGS_RELEASE])
  query = base.SourceQuery(title="Strobe (Original)", artist="deadmau5")
  assert source.search(query)[0].tags.composer == "Joel Zimmerman"


def test_discogs_ignores_credits_scoped_to_other_tracks(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """A release credit naming other tracks is not applied to this one."""
  release = {
      "extraartists": [{
          "name": "Someone Else",
          "role": "Remix",
          "tracks": "B2",
      }],
      "tracklist": [{
          "position": "A",
          "title": "Strobe",
          "extraartists": []
      }],
  }
  source = discogs_source(monkeypatch, [DISCOGS_SEARCH, release])
  query = base.SourceQuery(title="Strobe", artist="deadmau5")
  assert source.search(query)[0].tags.remixer is None


def test_discogs_splits_the_release_heading(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """Discogs writes "Artist - Title"; both halves are useful."""
  source = discogs_source(monkeypatch, [DISCOGS_SEARCH, {}])
  tags = source.search(base.SourceQuery(title="Strobe"))[0].tags
  assert tags.artist == "deadmau5"


def test_discogs_reports_label_and_catalogue_number(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """Label and catalogue number are shown even though nothing writes them."""
  source = discogs_source(monkeypatch, [DISCOGS_SEARCH, {}])
  extra = source.search(base.SourceQuery(title="Strobe"))[0].extra
  assert extra["label"] == "mau5trap"
  assert extra["catno"] == "MAU5015"


def test_discogs_prefers_style_over_genre(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """Discogs styles are the fine-grained taxonomy worth tagging."""
  source = discogs_source(monkeypatch, [DISCOGS_SEARCH, {}])
  assert source.search(
      base.SourceQuery(title="Strobe"))[0].tags.genre == "Progressive House"


def test_discogs_survives_a_failed_release_lookup(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """Losing credits must not lose the candidate."""
  monkeypatch.setenv(discogs.TOKEN_VAR, "token")

  class FailingLookup(FakeClient):
    """Fails every request after the first."""

    def get_json(self,
                 url: str,
                 params: Any = None,
                 headers: Any = None,
                 **kwargs: Any) -> Any:
      """Raises on the release lookup, succeeds on the search."""
      if "/releases/" in url:
        raise http.HttpError("boom")
      return super().get_json(url, params, headers, **kwargs)

  source = discogs.DiscogsSource(FailingLookup([DISCOGS_SEARCH]))
  results = source.search(base.SourceQuery(title="Strobe"))
  assert results
  assert results[0].tags.artist == "deadmau5"


def test_discogs_needs_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
  """Without a token the source reports itself unavailable."""
  monkeypatch.delenv(discogs.TOKEN_VAR, raising=False)
  assert not discogs.DiscogsSource(FakeClient({})).is_available()


@pytest.mark.parametrize("position,expected", [
    ("1", 1),
    ("A2", 2),
    ("B1", 1),
    ("", None),
    (None, None),
])
def test_discogs_reads_vinyl_positions(position: Any,
                                       expected: int | None) -> None:
  """Vinyl positions are "A2", where the letter is the side."""
  assert discogs.position_number(position) == expected


# ---------------------------------------------------------------- shared


def test_every_source_is_registered() -> None:
  """The registry names match the names precedence.toml uses."""
  from music_match import sources  # pylint: disable=import-outside-toplevel
  assert set(
      sources.known_names()) == {"discogs", "musicbrainz", "spotify", "itunes"}
  for name in sources.known_names():
    assert sources.build(name).name == name


def test_sources_ignore_an_unusable_query() -> None:
  """With no title there is nothing to search on, so nothing is asked."""
  empty = base.SourceQuery()
  client = FakeClient({})
  assert not itunes.ITunesSource(client).search(empty)
  assert not client.calls


def test_query_from_tags_falls_back_to_album_artist() -> None:
  """A file with only an album artist still yields a usable query."""
  from music_match.tagging.fields import TrackTags  # pylint: disable=import-outside-toplevel
  query = base.SourceQuery.from_tags(TrackTags(title="T", album_artist="AA"))
  assert query.artist == "AA"
  assert query.is_usable()


def test_require_names_the_variable_and_the_source() -> None:
  """A missing credential says which one and who needed it."""
  with pytest.raises(env.MissingCredential, match="MY_KEY"):
    env.require("MY_KEY", "somesource")
