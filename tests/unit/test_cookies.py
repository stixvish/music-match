"""Cookie persistence (SPEC.md §6)."""

import pytest

from music import pipeline
from music.acquire import QualityError, VideoRef, youtube
from music.config import Config, YouTubeConfig


@pytest.fixture
def jar(tmp_path, monkeypatch):
  """Point the cookie jar at a temp path."""
  path = tmp_path / "cookies.txt"
  monkeypatch.setattr(youtube, "cookie_path", lambda cfg=None: path)
  return path


class FakeJar(dict):
  """Stands in for yt-dlp's cookie jar."""

  def __init__(self, saves):
    """Record saves into the given list."""
    super().__init__()
    self._saves = saves

  def save(self, path):
    """Record the save and write something recognisable."""
    self._saves.append(path)
    from pathlib import Path

    Path(path).write_text("# Netscape HTTP Cookie File\n")


def test_the_browser_is_read_once_to_bootstrap(jar, monkeypatch):
  """Reading the browser is a bootstrap, not a routine."""
  saves: list[str] = []
  monkeypatch.setattr(
    "yt_dlp.cookies.extract_cookies_from_browser", lambda *a, **k: FakeJar(saves)
  )
  cfg = YouTubeConfig()
  first = youtube._cookie_jar(cfg)
  second = youtube._cookie_jar(cfg)
  assert first == second == jar
  assert len(saves) == 1, "the browser was read again when a jar already existed"


def test_an_existing_jar_is_never_replaced_silently(jar, monkeypatch):
  """yt-dlp writes rotations back into this file; re-reading undoes them.

  Discarding the jar each run and re-reading the browser restored the older
  browser copy and eventually failed authentication — on a machine where the
  browser was never used for YouTube at all.
  """
  jar.write_text("# Netscape HTTP Cookie File\n# rotated by yt-dlp\n")

  def explode(*_a, **_k):
    raise AssertionError("the browser must not be read when a jar exists")

  monkeypatch.setattr("yt_dlp.cookies.extract_cookies_from_browser", explode)
  assert youtube._cookie_jar(YouTubeConfig()) == jar
  assert "rotated by yt-dlp" in jar.read_text()


def test_refresh_is_the_recovery_path(jar, monkeypatch):
  """It exists for an auth failure, not for every run."""
  saves: list[str] = []
  jar.write_text("# stale\n")
  monkeypatch.setattr(
    "yt_dlp.cookies.extract_cookies_from_browser", lambda *a, **k: FakeJar(saves)
  )
  youtube.refresh_cookies()
  assert not jar.exists()
  youtube._cookie_jar(YouTubeConfig())
  assert len(saves) == 1


def test_a_configured_cookie_file_wins():
  """An exported file is used verbatim, wherever the user put it."""
  cfg = YouTubeConfig(cookie_file="~/somewhere/cookies.txt")
  assert youtube.cookie_path(cfg).name == "cookies.txt"
  assert "~" not in str(youtube.cookie_path(cfg))


def test_extraction_failure_falls_back_rather_than_losing_auth(jar, monkeypatch):
  """Per-call browser extraction still works; losing authentication does not."""
  monkeypatch.setattr(
    "yt_dlp.cookies.extract_cookies_from_browser",
    lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")),
  )
  assert youtube._cookie_jar(YouTubeConfig()) is None
  opts = youtube._base_opts(YouTubeConfig())
  assert opts.get("cookiesfrombrowser"), "fell back to no cookies at all"


def test_yt_dlp_is_given_a_file_so_it_can_write_rotations_back(jar, monkeypatch):
  """`YoutubeDL.save_cookies` only persists when `cookiefile` is set."""
  saves: list[str] = []
  monkeypatch.setattr(
    "yt_dlp.cookies.extract_cookies_from_browser", lambda *a, **k: FakeJar(saves)
  )
  opts = youtube._base_opts(YouTubeConfig())
  assert opts.get("cookiefile") == str(jar)
  assert not opts.get("cookiesfrombrowser")


# --- mid-run recovery ------------------------------------------------------


def test_a_downgraded_stream_retries_once_with_fresh_cookies(monkeypatch):
  attempts: list[str] = []
  refreshed: list[bool] = []

  def fake_download(video_id, dest, cfg, sink=None):  # noqa: ARG001
    attempts.append(video_id)
    if len(attempts) == 1:
      raise QualityError("best available 128 kbps is below the 256 kbps floor")
    return "ok"

  monkeypatch.setattr(pipeline, "download", fake_download)
  monkeypatch.setattr(pipeline, "refresh_cookies", lambda: refreshed.append(True))

  got = pipeline._download_with_fresh_cookies(
    VideoRef(video_id="x", title="T", channel="c", duration_s=1),
    Config(),
    None,
    pipeline.LogBuffer(),
    pipeline.Progress(),
  )
  assert got == "ok"
  assert len(attempts) == 2
  assert refreshed == [True]


def test_the_refresh_happens_once_per_run_not_once_per_track(monkeypatch):
  """Refreshing per failing track reads the keychain 2,329 times over a run."""
  refreshed: list[bool] = []
  state = pipeline.Progress()

  def always_bad(video_id, dest, cfg, sink=None):  # noqa: ARG001
    raise QualityError("still below the floor")

  monkeypatch.setattr(pipeline, "download", always_bad)
  monkeypatch.setattr(pipeline, "refresh_cookies", lambda: refreshed.append(True))

  for _ in range(5):
    with pytest.raises(QualityError):
      pipeline._download_with_fresh_cookies(
        VideoRef(video_id="x", title="T", channel="c", duration_s=1),
        Config(),
        None,
        pipeline.LogBuffer(),
        state,
      )
  assert len(refreshed) == 1, f"refreshed {len(refreshed)} times across one run"
