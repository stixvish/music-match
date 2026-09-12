"""Web API (tasks/todo.md t28-t30, SPEC.md §15)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from music import db
from music.web import create_app


@pytest.fixture(autouse=True)
def stub_spotify(monkeypatch):
  """Answer track-by-id lookups locally; no test here may reach the network."""
  from music.sources import spotify
  from music.sources.base import FieldCandidate

  monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-id")
  monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test-secret")
  monkeypatch.setattr(
    spotify.Spotify, "__init__", lambda self, conn, a, b: setattr(self, "_conn", conn)
  )
  monkeypatch.setattr(
    spotify.Spotify,
    "track",
    lambda self, track_id: [
      FieldCandidate(field="title", value="Lost", source="spotify"),
      FieldCandidate(field="artist", value="Frank Ocean", source="spotify"),
      FieldCandidate(field="album", value="channel ORANGE", source="spotify"),
    ],
  )


@pytest.fixture
def client(tmp_path, synth_audio):
  path = tmp_path / "w.db"
  conn = db.connect(path)
  db.migrate(conn)
  conn.execute(
    "INSERT INTO source_file (id, origin, staging_path, sha256, duration_s, channel)"
    " VALUES (1, 'youtube', ?, 'x', 2.0, 'Artist - Topic')",
    (str(synth_audio),),
  )
  conn.execute(
    "INSERT INTO track (id, source_file_id, status, genre_family,"
    " identity_confidence) VALUES (1, 1, 'review', 'pop', 0.5)"
  )
  conn.execute(
    "INSERT INTO review_queue (track_id, reason) VALUES (1, 'low_confidence')"
  )
  for field, value, source, by in [
    ("title", "A Song", "musicbrainz", "precedence"),
    ("artist", "An Artist", "musicbrainz", "precedence"),
    ("genre", "House", "discogs", "precedence"),
  ]:
    conn.execute(
      "INSERT INTO resolved_field (track_id, field, value, source, decided_by)"
      " VALUES (1,?,?,?,?)",
      (field, value, source, by),
    )
  for field, value, source in [
    ("genre", "House", "discogs"),
    ("genre", "Dance", "itunes"),
    ("genre", "Electronic", "essentia"),
  ]:
    conn.execute(
      "INSERT INTO field_candidate (track_id, field, value, source) VALUES (1,?,?,?)",
      (field, value, source),
    )
  conn.close()
  return TestClient(create_app(database=path))


def test_index_and_assets_load(client):
  assert client.get("/").status_code == 200
  assert "review" in client.get("/").text.lower()
  assert client.get("/app.css").status_code == 200
  assert client.get("/app.js").status_code == 200


def test_queue_lists_review_items(client):
  rows = client.get("/api/tracks?status=review").json()
  assert len(rows) == 1
  assert rows[0]["reason"] == "low_confidence"
  assert rows[0]["title"] == "A Song"


def test_track_returns_resolved_and_candidates(client):
  data = client.get("/api/track/1").json()
  assert data["track"]["genre_family"] == "pop"
  assert {r["field"] for r in data["resolved"]} == {"title", "artist", "genre"}
  genres = [c["source"] for c in data["candidates"] if c["field"] == "genre"]
  assert set(genres) == {"discogs", "itunes", "essentia"}


def test_missing_track_is_404(client):
  assert client.get("/api/track/999").status_code == 404


def test_editing_a_field_marks_it_manual(client):
  """A human decided this; no resolver run may overrule it (SPEC.md §15)."""
  client.post("/api/track/1/field", json={"field": "genre", "value": "Melodic Techno"})
  resolved = {r["field"]: r for r in client.get("/api/track/1").json()["resolved"]}
  assert resolved["genre"]["value"] == "Melodic Techno"
  assert resolved["genre"]["decided_by"] == "manual"


def test_accepting_clears_the_queue(client):
  assert client.get("/api/tracks?status=review").json()
  client.post("/api/track/1/accept")
  assert client.get("/api/tracks?status=review").json() == []


def test_url_override_is_parsed_and_fetched(client):
  """Renamed from `..._is_parsed_and_stored`, which is what was wrong with it.

  Storing the link was all it ever did: nothing read the row back, so the one
  lever meant to rescue the hardest review items changed nothing at all while
  reporting success.
  """
  r = client.post(
    "/api/track/1/url",
    json={"url": "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"},
  )
  assert r.status_code == 200
  assert r.json()["provider"] == "spotify"
  assert r.json()["id"] == "4cOdK2wGLETKBW3PvgPWqT"
  assert r.json()["fields"] == 3


def test_unrecognised_url_is_rejected(client):
  r = client.post("/api/track/1/url", json={"url": "https://example.com/nope"})
  assert r.status_code == 400


def test_audio_is_served_for_preview(client):
  """Audio preview is non-negotiable.

  The common failure is a plausible match that is the wrong recording, and you
  have to hear it (SPEC.md §15).
  """
  r = client.get("/api/audio/1")
  assert r.status_code == 200
  assert len(r.content) > 1000


def test_audio_content_type_is_one_a_browser_decodes(client):
  """A 200 with bytes is not enough — the media type has to be decodable.

  `mimetypes` guesses `audio/mp4a-latm` for .m4a, which names a raw LATM
  stream rather than the MP4 container. Chrome returns an empty `canPlayType`
  for it, so the player sits silently at 0:00 with no error anywhere
  (SPEC.md §15). The assertion above passed throughout, which is exactly why
  this one exists.
  """
  assert client.get("/api/audio/1").headers["content-type"] == "audio/mp4"


def test_audio_prefers_the_source_over_the_published_file(client):
  """The download source is served even once an AIFF exists.

  No browser decodes AIFF, and the source is several times smaller.
  """
  client.post("/api/track/1/accept")
  published = Path(client.get("/api/tracks").json()[0]["published_path"])
  assert published.suffix == ".aiff"
  r = client.get("/api/audio/1")
  assert r.headers["content-type"] == "audio/mp4"
  assert len(r.content) < published.stat().st_size


def test_audio_falls_back_to_a_preview_when_the_source_is_gone(client, tmp_path):
  """Clearing staging must not cost the ability to audition the library."""
  client.post("/api/track/1/accept")
  conn = db.connect(tmp_path / "w.db")
  conn.execute("UPDATE source_file SET staging_path=?", (str(tmp_path / "gone.m4a"),))
  conn.commit()
  conn.close()
  r = client.get("/api/audio/1")
  assert r.status_code == 200
  assert r.headers["content-type"] == "audio/mp4"
  assert len(r.content) > 1000


def test_audio_404s_when_nothing_is_on_disk(client, tmp_path):
  conn = db.connect(tmp_path / "w.db")
  conn.execute("UPDATE source_file SET staging_path=?", (str(tmp_path / "gone.m4a"),))
  conn.commit()
  conn.close()
  assert client.get("/api/audio/1").status_code == 404


def test_published_tracks_stay_visible_with_their_path(client):
  """A published track is reviewable after the fact, not filtered out of sight."""
  client.post("/api/track/1/accept")
  rows = client.get("/api/tracks?status=published").json()
  assert len(rows) == 1
  assert rows[0]["published_path"].endswith(".aiff")


# --- serving ---------------------------------------------------------------


def test_free_port_finds_an_open_one():
  import socket

  from music.cli import _free_port

  with socket.socket() as taken:
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    busy = taken.getsockname()[1]
    assert _free_port(busy, tries=1) is None
    assert _free_port(busy) != busy


def test_free_port_returns_the_preferred_one_when_open():
  from music.cli import _free_port

  assert _free_port(0, tries=1) == 0 or _free_port(8899, tries=1) is not None


def test_accepting_a_reviewed_track_publishes_it(client, tmp_path):
  """Approving a reviewed track publishes it.

  A low-confidence track is never arbitrated by the pipeline (SPEC.md §12), so
  approving one must do that work — otherwise accept clears the queue and
  leaves an untagged file that was never published.
  """
  before = client.get("/api/track/1").json()
  assert not any(r["field"] == "label" for r in before["resolved"])

  r = client.post("/api/track/1/accept").json()
  assert r["ok"], r
  after = client.get("/api/tracks").json()[0]
  assert after["status"] == "published"
  assert after["published_path"]


def test_accept_preserves_a_manual_edit(client):
  client.post("/api/track/1/field", json={"field": "genre", "value": "Mine"})
  client.post("/api/track/1/accept")
  resolved = {r["field"]: r for r in client.get("/api/track/1").json()["resolved"]}
  assert resolved["genre"]["value"] == "Mine"
  assert resolved["genre"]["decided_by"] == "manual"


def test_queue_row_falls_back_to_the_normalised_name(client):
  rows = client.get("/api/tracks").json()
  assert "norm_title" in rows[0]
  assert "norm_artist" in rows[0]


def test_artwork_endpoint_reports_absence_clearly(client):
  """A reviewer must be able to see the art, not just a URL (SPEC.md §15)."""
  # nothing published yet in this fixture
  assert client.get("/api/artwork/1").status_code == 404


def test_artwork_is_served_once_published(client):
  from music.publish import tag

  client.post("/api/track/1/accept")
  path = client.get("/api/tracks").json()[0]["published_path"]
  tags = tag.read(__import__("pathlib").Path(path))
  tags.artwork = b"\xff\xd8\xff\xe0" + b"\x00" * 64
  tag.write(__import__("pathlib").Path(path), tags)

  r = client.get("/api/artwork/1")
  assert r.status_code == 200
  assert r.headers["content-type"] == "image/jpeg"
  assert r.content == tags.artwork


# --- search ----------------------------------------------------------------


def test_search_matches_the_resolved_title(client):
  assert len(client.get("/api/tracks?q=song").json()) == 1
  assert client.get("/api/tracks?q=nothinglikethis").json() == []


def test_search_matches_the_resolved_artist(client):
  assert len(client.get("/api/tracks?q=an artist").json()) == 1


def test_search_matches_the_name_the_track_was_searched_under(client, tmp_path):
  """Finding a bad match means searching for what YouTube called it."""
  conn = db.connect(tmp_path / "w.db")
  conn.execute("UPDATE track SET norm_title = 'Totally Different Upload'")
  conn.commit()
  conn.close()
  assert len(client.get("/api/tracks?q=totally different").json()) == 1


def test_search_is_case_insensitive(client):
  assert len(client.get("/api/tracks?q=A SONG").json()) == 1


def test_search_combines_with_the_status_filter(client):
  assert len(client.get("/api/tracks?q=song&status=review").json()) == 1
  assert client.get("/api/tracks?q=song&status=published").json() == []


def test_an_empty_query_returns_everything(client):
  assert len(client.get("/api/tracks?q=").json()) == 1


def test_a_wildcard_in_the_query_is_not_a_wildcard(client):
  """`%` is a LIKE metacharacter; a user typing it means a literal percent."""
  assert client.get("/api/tracks?q=%").json() == []


# --- pasted links ----------------------------------------------------------


def test_an_unsupported_link_kind_says_so(client):
  """Silence was the original bug: the endpoint returned ok and did nothing."""
  r = client.post(
    "/api/track/1/url",
    json={"url": "https://open.spotify.com/album/4m2880jivSbbyEGAKfITCa"},
  )
  assert r.status_code == 422
  assert "not supported" in r.json()["detail"]


def test_a_pasted_link_rewrites_the_fields(client):
  """A link is authoritative: it is fetched and applied, not merely recorded."""
  r = client.post(
    "/api/track/1/url",
    json={"url": "https://open.spotify.com/track/3GZD6HmiNUhxXYf8Gch723"},
  )
  assert r.status_code == 200, r.text
  assert r.json()["title"] == "Lost"

  resolved = {f["field"]: f for f in client.get("/api/track/1").json()["resolved"]}
  assert resolved["title"]["value"] == "Lost"
  assert resolved["title"]["decided_by"] == "url_override"


def test_a_pasted_link_clears_the_stale_reason_but_keeps_the_track_visible(client):
  """The link settles the identity; accepting it is still yours to do.

  Dropping the track out of the review list entirely would hide it right when
  you want to glance at the corrected fields and press Accept — so the reason
  goes and the row stays.
  """
  assert client.get("/api/tracks?status=review").json()[0]["reason"]

  client.post(
    "/api/track/1/url",
    json={"url": "https://open.spotify.com/track/3GZD6HmiNUhxXYf8Gch723"},
  )
  rows = client.get("/api/tracks?status=review").json()
  assert len(rows) == 1
  assert rows[0]["reason"] is None
  assert rows[0]["identity_confidence"] == 1.0


def test_a_nonsense_link_is_still_rejected(client):
  assert client.post("/api/track/1/url", json={"url": "hello"}).status_code == 400


# --- deleting a track ------------------------------------------------------


def test_rejecting_a_track_over_http(client):
  """The review ui is where a track is judged, so it is where it is dropped."""
  r = client.post("/api/track/1/reject")
  assert r.status_code == 200, r.text
  body = r.json()
  assert body["ok"]
  assert body["label"] == "An Artist - A Song"

  rows = client.get("/api/tracks?status=skipped").json()
  assert len(rows) == 1
  assert rows[0]["published_path"] is None


def test_a_rejected_track_leaves_the_review_queue(client):
  assert client.get("/api/tracks?status=review").json()
  client.post("/api/track/1/reject")
  assert client.get("/api/tracks?status=review").json() == []


def test_rejecting_an_unknown_track_is_404(client):
  assert client.post("/api/track/999/reject").status_code == 404
