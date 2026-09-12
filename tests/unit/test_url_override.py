"""Pasted-link parsing (tasks/todo.md t22)."""

import pytest

from music.sources.url_override import Provider, is_supported, parse


@pytest.mark.parametrize(
  ("url", "provider", "kind", "identifier"),
  [
    (
      "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
      Provider.SPOTIFY,
      "track",
      "4cOdK2wGLETKBW3PvgPWqT",
    ),
    (
      "https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT?si=abc123",
      Provider.SPOTIFY,
      "track",
      "4cOdK2wGLETKBW3PvgPWqT",
    ),
    (
      "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
      Provider.SPOTIFY,
      "track",
      "4cOdK2wGLETKBW3PvgPWqT",
    ),
    (
      "https://open.spotify.com/album/1ATL5GLyefJaxhQzSPVrLX",
      Provider.SPOTIFY,
      "album",
      "1ATL5GLyefJaxhQzSPVrLX",
    ),
    (
      "https://musicbrainz.org/recording/16c83afd-1111-2222-3333-444444444444",
      Provider.MUSICBRAINZ,
      "recording",
      "16c83afd-1111-2222-3333-444444444444",
    ),
    (
      "https://www.discogs.com/release/249504-Rick-Astley-Never-Gonna-Give-You-Up",
      Provider.DISCOGS,
      "release",
      "249504",
    ),
    (
      "https://www.beatport.com/track/strobe/1234567",
      Provider.BEATPORT,
      "track",
      "1234567",
    ),
  ],
)
def test_parses_known_links(url, provider, kind, identifier):
  ref = parse(url)
  assert ref is not None
  assert (ref.provider, ref.kind, ref.identifier) == (provider, kind, identifier)


@pytest.mark.parametrize(
  "url",
  [
    "",
    "   ",
    "not a url",
    "https://example.com/track/123",
    "https://open.spotify.com/track/tooshort",
    "https://youtube.com/watch?v=abc",
  ],
)
def test_rejects_unknown_links(url):
  assert parse(url) is None
  assert not is_supported(url)


def test_surrounding_text_is_tolerated():
  """Users paste links with words around them."""
  ref = parse("this one: https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT thanks")
  assert ref is not None
  assert ref.identifier == "4cOdK2wGLETKBW3PvgPWqT"
