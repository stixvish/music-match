"""Live checks against the real APIs and models (SPEC.md §19).

Excluded from CI by the `live` marker and run nightly, because the thing they
catch — a source changing its response shape, or an essentia model failing to
download — is invisible until a multi-hour run is already underway.

Credentialed sources skip rather than fail when the secret is absent, so the
nightly is useful without any secrets configured and becomes more useful with
them.
"""

import pytest

from music import config
from music.sources.base import Identity

pytestmark = pytest.mark.live

IDENTITY = Identity(artist="Pitbull", title="Give Me Everything", duration_s=252.0)


def credentials():
  return config.load().credentials


# --- no credentials required -----------------------------------------------


def test_musicbrainz_still_answers(tmp_path):
  """Free, keyless, and the identity source everything else hangs off."""
  from music import db
  from music.sources.musicbrainz import MusicBrainz

  conn = db.connect(tmp_path / "c.db")
  db.migrate(conn)
  match, candidates = MusicBrainz(conn).evaluate(IDENTITY)
  fields = {c.field for c in candidates}
  assert "title" in fields, f"musicbrainz returned no title: {fields}"
  assert "artist" in fields
  assert match.evidence is not None


def test_musicbrainz_artist_credit_still_carries_join_phrases(tmp_path):
  """`split_credit` depends on this; without it every feature is silently lost."""
  from music import db
  from music.sources.musicbrainz import MusicBrainz, split_credit

  conn = db.connect(tmp_path / "c.db")
  db.migrate(conn)
  source = MusicBrainz(conn)
  recordings = source.search_recordings(
    Identity(artist="Justice", title="Neverender"), limit=3
  )
  assert recordings, "no recordings returned"
  credited = [r for r in recordings if r.get("artist-credit")]
  assert credited, "no artist-credit on any result"
  act, _featured = split_credit(credited[0]["artist-credit"])
  assert act, "artist-credit parsed to an empty act"


def test_itunes_still_answers(tmp_path):
  """Genre and artwork both lead from iTunes; a shape change empties both."""
  from music import db
  from music.sources.itunes import ITunes

  conn = db.connect(tmp_path / "c.db")
  db.migrate(conn)
  got = {c.field: c.value for c in ITunes(conn).lookup(IDENTITY)}
  assert got.get("title"), f"itunes returned nothing usable: {got}"
  assert got.get("genre"), "itunes stopped returning a genre"
  assert got.get("artwork_url", "").startswith("http")


def test_itunes_still_calls_bollywood_bollywood(tmp_path):
  """The entire regional routing rests on this one label."""
  from music import db
  from music.sources.itunes import ITunes

  conn = db.connect(tmp_path / "c.db")
  db.migrate(conn)
  got = {
    c.field: c.value
    for c in ITunes(conn).lookup(Identity(artist="Arijit Singh", title="Channa Mereya"))
  }
  assert got.get("genre", "").casefold() in {"bollywood", "indian pop", "telugu"}, (
    f"itunes genre for a bollywood track was {got.get('genre')!r}"
  )


def test_cover_art_archive_still_serves_front_covers(tmp_path):
  """The fallback when no catalogue holds the release we tagged."""
  from music import db
  from music.sources.musicbrainz import MusicBrainz

  conn = db.connect(tmp_path / "c.db")
  db.migrate(conn)
  source = MusicBrainz(conn)
  recordings = source.search_recordings(IDENTITY, limit=1)
  assert recordings
  full = source.recording(str(recordings[0]["id"]))
  releases = source.releases_for(full)
  assert releases, "no releases parsed"
  url = source.cover_art_url(releases[0].release_id, releases[0].release_group_id)
  assert url == "" or url.startswith("https://coverartarchive.org/")


# --- credentialed ----------------------------------------------------------


def test_spotify_track_lookup_still_works(tmp_path):
  """Spotify leads every identity field outside electronic."""
  creds = credentials()
  if not (creds.get("SPOTIFY_CLIENT_ID") and creds.get("SPOTIFY_CLIENT_SECRET")):
    pytest.skip("no spotify credentials")
  from music import db
  from music.sources.spotify import Spotify

  conn = db.connect(tmp_path / "c.db")
  db.migrate(conn)
  source = Spotify(conn, creds["SPOTIFY_CLIENT_ID"], creds["SPOTIFY_CLIENT_SECRET"])
  # Frank Ocean - Lost, the track the url-override path was built against
  got = {c.field: c.value for c in source.track("3GZD6HmiNUhxXYf8Gch723")}
  assert got.get("title") == "Lost"
  assert got.get("album") == "channel ORANGE"
  assert got.get("isrc"), "spotify stopped returning an isrc"


def test_discogs_still_answers(tmp_path):
  creds = credentials()
  if not creds.get("DISCOGS_TOKEN"):
    pytest.skip("no discogs token")
  from music import db
  from music.sources.discogs import Discogs

  conn = db.connect(tmp_path / "c.db")
  db.migrate(conn)
  got = {
    c.field: c.value for c in Discogs(conn, creds["DISCOGS_TOKEN"]).lookup(IDENTITY)
  }
  assert got, "discogs returned nothing at all"


def test_acoustid_still_answers(tmp_path, synth_audio):
  creds = credentials()
  if not creds.get("ACOUSTID_API_KEY"):
    pytest.skip("no acoustid key")
  from music import db
  from music.sources.acoustid import AcoustId, fingerprint

  conn = db.connect(tmp_path / "c.db")
  db.migrate(conn)
  from music.sources.acoustid import FingerprintError

  try:
    print_ = fingerprint(synth_audio)
  except FingerprintError as exc:
    pytest.skip(f"fpcalc cannot read the synthesized fixture: {exc}")
  assert print_.duration > 0, "fpcalc produced no duration"
  # a synthesized sine matches nothing; the point is that the call succeeds
  AcoustId(conn, creds["ACOUSTID_API_KEY"]).lookup_fingerprint(print_)
