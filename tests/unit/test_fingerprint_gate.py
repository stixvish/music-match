"""The AcoustID path scores what it links to (SPEC.md §12)."""

from music.identify import Evidence
from music.resolve import Resolver
from music.sources.acoustid import AcoustIdMatch
from music.sources.base import FieldCandidate, Identity


class FakeAcoustId:
  """Returns one high-scoring match linking the given recording ids."""

  def __init__(self, recording_ids):
    """Store the ids to link."""
    self._ids = recording_ids

  def identify(self, path):  # noqa: ARG002
    """Return the canned match."""
    return [
      AcoustIdMatch(
        acoustid="a", score=0.98, recording_ids=tuple(self._ids), title="", artist=""
      )
    ]


class FakeMusicBrainz:
  """Serves canned recordings by id, and a text-search fallback."""

  def __init__(self, recordings, text_result=None):
    """Store the canned recordings and optional text-search answer."""
    self._recordings = recordings
    self._text = text_result
    self.text_called = False

  def recording(self, mbid):
    """Return a canned recording."""
    return self._recordings[mbid]

  def releases_for(self, recording):  # noqa: ARG002
    """No releases, so the release bonus never applies."""
    return []

  def candidates_from(self, recording, identity):  # noqa: ARG002
    """One candidate carrying the recording title."""
    return [
      FieldCandidate(field="title", value=recording["title"], source="musicbrainz")
    ]

  def evaluate(self, identity):  # noqa: ARG002
    """The text-search fallback, recording that it was reached."""
    self.text_called = True
    from music.identify import Match

    if self._text is None:
      return Match(Evidence.NONE), []
    return (
      Match(evidence=Evidence.TEXT, search_score=95, duration_delta_s=0.0),
      [FieldCandidate(field="title", value=self._text, source="musicbrainz")],
    )


def recording(title, artist):
  return {"title": title, "artist-credit": [{"name": artist}], "length": 200000}


IDENTITY = Identity(artist="Morgan Wallen", title="Last Night", duration_s=200.0)


def test_an_unrelated_linked_recording_is_rejected(tmp_path):
  """The real failure: the fingerprint linked to Metro Station's California."""
  audio = tmp_path / "a.m4a"
  audio.write_bytes(b"x")
  mb = FakeMusicBrainz({"r1": recording("California", "Metro Station")})
  resolver = Resolver(None, mb, FakeAcoustId(["r1"]))

  result = resolver.resolve(IDENTITY, audio)
  assert result.match.evidence is not Evidence.ACOUSTID
  assert mb.text_called, "a rejected fingerprint must fall back to text search"


def test_rejection_falls_through_to_the_text_result(tmp_path):
  audio = tmp_path / "a.m4a"
  audio.write_bytes(b"x")
  mb = FakeMusicBrainz(
    {"r1": recording("California", "Metro Station")}, text_result="Last Night"
  )
  resolver = Resolver(None, mb, FakeAcoustId(["r1"]))

  result = resolver.resolve(IDENTITY, audio)
  assert [c.value for c in result.candidates] == ["Last Night"]


def test_the_matching_recording_is_chosen_over_an_earlier_one(tmp_path):
  """Position in the AcoustID response carries no meaning."""
  audio = tmp_path / "a.m4a"
  audio.write_bytes(b"x")
  mb = FakeMusicBrainz(
    {
      "r1": recording("California", "Metro Station"),
      "r2": recording("Last Night", "Morgan Wallen"),
    }
  )
  resolver = Resolver(None, mb, FakeAcoustId(["r1", "r2"]))

  result = resolver.resolve(IDENTITY, audio)
  assert result.match.evidence is Evidence.ACOUSTID
  assert [c.value for c in result.candidates] == ["Last Night"]


def test_a_good_first_recording_is_still_used(tmp_path):
  audio = tmp_path / "a.m4a"
  audio.write_bytes(b"x")
  mb = FakeMusicBrainz({"r1": recording("Last Night", "Morgan Wallen")})
  resolver = Resolver(None, mb, FakeAcoustId(["r1"]))

  result = resolver.resolve(IDENTITY, audio)
  assert result.match.evidence is Evidence.ACOUSTID
  assert not mb.text_called
