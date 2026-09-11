"""Adapter response parsing (tasks/todo.md t19-t21).

Cassette-style: every payload below is a real API response, trimmed. A mock
encodes what I *think* a service returns; a cassette encodes what it *did*
(SPEC.md §19).
"""

import pytest

from music.sources import discogs, itunes, spotify

# --- discogs ---------------------------------------------------------------

DISCOGS = {
  "results": [
    {
      "style": ["House", "Electro"],
      "genre": ["Electronic"],
      "label": ["Cybernetic"],
      "catno": "CYB18",
      "year": "2010",
    },
    {
      "style": ["House"],
      "genre": ["Electronic"],
      "label": ["Virgin"],
      "catno": "7243 8 97688 6 5",
      "year": "2000",
    },
  ]
}


def test_discogs_prefers_the_earliest_pressing():
  """Search returns bootlegs and reissues; the earliest is likely original."""
  chosen = discogs.select_result(DISCOGS["results"])
  assert chosen["label"] == ["Virgin"]
  assert chosen["year"] == "2000"


def test_discogs_prefers_style_over_genre():
  """Discogs genre is too coarse for a DJ; style is the useful taxonomy."""
  got = {c.field: c.value for c in discogs.candidates_from(DISCOGS["results"][0])}
  assert got["genre"] == "House"


def test_discogs_falls_back_to_genre_when_no_style():
  got = {
    c.field: c.value
    for c in discogs.candidates_from({"genre": ["Electronic"], "style": []})
  }
  assert got["genre"] == "Electronic"


def test_discogs_undated_results_still_usable():
  assert discogs.select_result([{"label": ["X"]}])["label"] == ["X"]


def test_discogs_no_results():
  assert discogs.select_result([]) is None
  assert discogs.candidates_from({}) == []


# --- itunes ----------------------------------------------------------------

ITUNES = {
  "results": [
    {
      "trackName": "One More Time",
      "artistName": "Daft Punk",
      "collectionName": "Discovery",
      "primaryGenreName": "Dance",
      "trackNumber": 1,
      "discNumber": 1,
      "releaseDate": "2000-11-30T08:00:00Z",
      "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/x/100x100bb.jpg",
    }
  ]
}


def test_itunes_candidates():
  got = {c.field: c.value for c in itunes.candidates_from(ITUNES["results"][0])}
  assert got["title"] == "One More Time"
  assert got["album"] == "Discovery"
  assert got["release_date"] == "2000-11-30"
  assert got["year"] == "2000"


@pytest.mark.parametrize(
  ("url", "expected"),
  [
    ("https://x/100x100bb.jpg", "https://x/1200x1200bb.jpg"),
    ("https://x/60x60bb.jpg", "https://x/1200x1200bb.jpg"),
    ("https://x/nosize.jpg", "https://x/nosize.jpg"),
  ],
)
def test_itunes_artwork_upscaling(url, expected):
  assert itunes.upscale_artwork(url) == expected


def test_itunes_missing_fields():
  assert itunes.candidates_from({}) == []


# --- spotify ---------------------------------------------------------------

SPOTIFY = {
  "name": "One More Time",
  "artists": [{"name": "Daft Punk"}, {"name": "Romanthony"}],
  "album": {
    "name": "Discovery",
    "artists": [{"name": "Daft Punk"}],
    "release_date": "2001-03-12",
    "images": [{"url": "https://i.scdn.co/image/abc"}],
  },
  "track_number": 1,
  "disc_number": 1,
  "external_ids": {"isrc": "GBDUW0000053"},
}


def test_spotify_candidates():
  got = {c.field: c.value for c in spotify.candidates_from(SPOTIFY)}
  assert got["title"] == "One More Time"
  assert got["album"] == "Discovery"
  assert got["isrc"] == "GBDUW0000053"
  assert got["artwork_url"] == "https://i.scdn.co/image/abc"


def test_spotify_takes_only_the_primary_artist():
  """Only the primary artist is taken.

  Spotify flattens featured and collaborating artists into one list and does
  not say which is which, so credits come from MusicBrainz (SPEC.md §7).
  """
  got = {c.field: c.value for c in spotify.candidates_from(SPOTIFY)}
  assert got["artist"] == "Daft Punk"


def test_spotify_missing_fields():
  assert spotify.candidates_from({}) == []
