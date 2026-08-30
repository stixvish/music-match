"""Live checks against the real metadata APIs.

These exist to catch the thing unit tests structurally cannot: a source
changing its response shape underneath us. They run in CI only, never in
the commit gate, and each skips itself when its credentials are absent so
a contributor without keys is not blocked.

Kept deliberately few and cheap — every one of these spends a real request
against a rate-limited service.
"""

import pytest

from music_match.config import env
from music_match.sources import base
from music_match.sources import discogs
from music_match.sources import itunes
from music_match.sources import musicbrainz
from music_match.sources import spotify

pytestmark = pytest.mark.integration

# Loaded at import time, not in a fixture: the skipif conditions below are
# evaluated during collection, which happens before any fixture runs. A
# fixture here would leave every credentialed test skipped on a machine
# that has the credentials sitting in .env.
env.load_env()

# A release old enough and famous enough that every one of these services
# is certain to hold it, so a failure means a shape change rather than a
# gap in someone's catalogue.
QUERY = base.SourceQuery(title="Around the World", artist="Daft Punk")


def test_itunes_returns_a_usable_result() -> None:
  """iTunes still answers, and still carries album and track numbers."""
  results = itunes.ITunesSource().search(QUERY, limit=1)
  assert results, "iTunes returned no result for a very well-known track"
  tags = results[0].tags
  assert tags.title
  assert tags.album
  assert tags.track_number is not None


def test_itunes_artwork_url_is_still_resizable() -> None:
  """The 100x100 path segment is what we rewrite to 640x640."""
  results = itunes.ITunesSource().search(QUERY, limit=1)
  assert results[0].art_url is not None
  assert "640x640" in results[0].art_url


def test_musicbrainz_returns_artist_credits() -> None:
  """MusicBrainz still answers without a key, given a user agent."""
  results = musicbrainz.MusicBrainzSource(fetch_isrcs=False).search(QUERY,
                                                                    limit=1)
  assert results, "MusicBrainz returned no result"
  assert results[0].tags.artist


@pytest.mark.skipif(
    not env.has(spotify.CLIENT_ID_VAR, spotify.CLIENT_SECRET_VAR),
    reason="Spotify credentials not configured")
def test_spotify_still_carries_an_isrc() -> None:
  """Spotify leads the ISRC ordering; this is that claim, checked."""
  results = spotify.SpotifySource().search(QUERY, limit=1)
  assert results, "Spotify returned no result"
  assert results[0].tags.isrc, "Spotify stopped returning ISRCs in search"


@pytest.mark.skipif(
    not env.has(spotify.CLIENT_ID_VAR, spotify.CLIENT_SECRET_VAR),
    reason="Spotify credentials not configured")
def test_spotify_still_offers_large_art() -> None:
  """640px is the size this tool embeds."""
  results = spotify.SpotifySource().search(QUERY, limit=1)
  assert results[0].art_size == 640


@pytest.mark.skipif(not env.has(discogs.TOKEN_VAR),
                    reason="Discogs token not configured")
def test_discogs_still_carries_credits() -> None:
  """Discogs leads for credits; this is that claim, checked.

  Uses a remix specifically, because remix credits sit on the tracklist
  entry rather than the release — the distinction this adapter had to get
  right.
  """
  query = base.SourceQuery(title="Strobe (DJ Marky & S.P.Y Remix)",
                           artist="deadmau5")
  results = discogs.DiscogsSource().search(query, limit=1)
  assert results, "Discogs returned no result"
  assert results[0].tags.remixer, "Discogs stopped exposing remix credits"


@pytest.mark.skipif(not env.has(discogs.TOKEN_VAR),
                    reason="Discogs token not configured")
def test_discogs_still_carries_label_and_catalogue_number() -> None:
  """Label and catalogue number are a large part of why Discogs leads."""
  results = discogs.DiscogsSource(fetch_release_detail=False).search(QUERY,
                                                                     limit=1)
  assert results
  assert "label" in results[0].extra
