"""Work-relationship credits (SPEC.md §2, composer/lyricist).

The payload below is a real MusicBrainz work response, trimmed.
"""

from music.sources.musicbrainz import parse_credits

# real response for the work "One More Time"
ONE_MORE_TIME = {
  "title": "One More Time",
  "relations": [
    {"type": "composer", "artist": {"name": "Thomas Bangalter"}},
    {"type": "writer", "artist": {"name": "Thomas Bangalter"}},
    {"type": "writer", "artist": {"name": "Guy-Manuel de Homem-Christo"}},
    {"type": "writer", "artist": {"name": "Anthony Wayne Moore"}},
  ],
}


def test_explicit_composer_beats_writer():
  """An explicit composer credit wins over a bare writer credit.

  `writer` means "wrote it" without saying which half, so it is only used when
  no explicit composer exists.
  """
  got = {c.field: c.value for c in parse_credits(ONE_MORE_TIME)}
  assert got["composer"] == "Thomas Bangalter"


def test_writer_is_used_when_no_composer_exists():
  work = {"relations": [{"type": "writer", "artist": {"name": "A Writer"}}]}
  assert {c.field: c.value for c in parse_credits(work)}["composer"] == "A Writer"


def test_lyricist_is_separate():
  work = {
    "relations": [
      {"type": "composer", "artist": {"name": "C"}},
      {"type": "lyricist", "artist": {"name": "L"}},
    ]
  }
  got = {c.field: c.value for c in parse_credits(work)}
  assert (got["composer"], got["lyricist"]) == ("C", "L")


def test_multiple_credits_are_joined():
  work = {
    "relations": [
      {"type": "composer", "artist": {"name": "A"}},
      {"type": "composer", "artist": {"name": "B"}},
    ]
  }
  assert {c.field: c.value for c in parse_credits(work)}["composer"] == "A, B"


def test_unrelated_relation_types_are_ignored():
  work = {"relations": [{"type": "producer", "artist": {"name": "P"}}]}
  assert parse_credits(work) == []


def test_no_relations():
  assert parse_credits({}) == []
  assert parse_credits({"relations": []}) == []
