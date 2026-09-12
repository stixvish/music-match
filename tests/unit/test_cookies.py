"""Cookie freshness (SPEC.md §6)."""

import pytest

from music import pipeline
from music.acquire import QualityError, VideoRef, youtube
from music.config import Config, YouTubeConfig


def test_a_jar_is_reused_within_one_run(monkeypatch):
  """Once per run, not once per track.

  Per-call extraction unlocks the keychain for every single download.
  """
  calls = []

  class FakeJar(dict):
    def save(self, path):
      calls.append(path)

  monkeypatch.setattr(youtube, "_COOKIE_JAR", None)
  monkeypatch.setattr(
    "yt_dlp.cookies.extract_cookies_from_browser", lambda *a, **k: FakeJar()
  )
  cfg = YouTubeConfig()
  first = youtube._cookie_jar(cfg)
  second = youtube._cookie_jar(cfg)
  assert first == second
  assert len(calls) == 1, "the browser was read more than once in a run"


def test_refresh_forces_a_new_extraction(monkeypatch):
  """YouTube rotates session cookies, and `music serve` stays up for days.

  The jar used to be cached for the life of the *process* while its docstring
  claimed the life of the *run*.
  """
  calls = []

  class FakeJar(dict):
    def save(self, path):
      calls.append(path)

  monkeypatch.setattr(youtube, "_COOKIE_JAR", None)
  monkeypatch.setattr(
    "yt_dlp.cookies.extract_cookies_from_browser", lambda *a, **k: FakeJar()
  )
  cfg = YouTubeConfig()
  first = youtube._cookie_jar(cfg)
  youtube.refresh_cookies()
  second = youtube._cookie_jar(cfg)
  assert first != second
  assert not first.exists(), "the stale jar was left on disk"
  assert len(calls) == 2


def test_extraction_failure_falls_back_rather_than_losing_auth(monkeypatch):
  """Per-call extraction still works; losing authentication does not."""
  monkeypatch.setattr(youtube, "_COOKIE_JAR", None)
  monkeypatch.setattr(
    "yt_dlp.cookies.extract_cookies_from_browser",
    lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")),
  )
  assert youtube._cookie_jar(YouTubeConfig()) is None
  opts = youtube._base_opts(YouTubeConfig())
  assert opts.get("cookiesfrombrowser"), "fell back to no cookies at all"


# --- mid-run rotation ------------------------------------------------------


def test_a_downgraded_stream_retries_once_with_fresh_cookies(monkeypatch):
  """A run takes hours; cookies can rotate part way through.

  The symptom is specific — the best available stream drops below the bitrate
  floor because YouTube is serving unauthenticated formats — so that signal is
  what triggers a refresh, rather than a guessed interval.
  """
  attempts = []
  refreshed = []

  def fake_download(video_id, dest, cfg, sink=None):  # noqa: ARG001
    attempts.append(video_id)
    if len(attempts) == 1:
      raise QualityError("best available 128 kbps is below the 256 kbps floor")
    return "ok"

  monkeypatch.setattr(pipeline, "download", fake_download)
  monkeypatch.setattr(pipeline, "refresh_cookies", lambda: refreshed.append(True))

  ref = VideoRef(video_id="x", title="T", channel="c", duration_s=1)
  got = pipeline._download_with_fresh_cookies(
    ref, Config(), None, pipeline.LogBuffer(), pipeline.Progress()
  )
  assert got == "ok"
  assert len(attempts) == 2
  assert refreshed == [True]


def test_it_gives_up_after_one_retry(monkeypatch):
  """Fresh cookies not helping means the account, not the jar — say so."""
  refreshed = []

  def always_bad(video_id, dest, cfg, sink=None):  # noqa: ARG001
    raise QualityError("still below the floor")

  monkeypatch.setattr(pipeline, "download", always_bad)
  monkeypatch.setattr(pipeline, "refresh_cookies", lambda: refreshed.append(True))

  with pytest.raises(QualityError):
    pipeline._download_with_fresh_cookies(
      VideoRef(video_id="x", title="T", channel="c", duration_s=1),
      Config(),
      None,
      pipeline.LogBuffer(),
      pipeline.Progress(),
    )
  assert len(refreshed) == 1, "refreshed on a loop instead of surfacing it"


def test_the_refresh_happens_once_per_run_not_once_per_track():
  """If every track is downgraded the jar is not the problem.

  Refreshing per failing track would read the keychain 2,300 times over a full
  library and turn one problem into a second one.
  """
  refreshed = []
  state = pipeline.Progress()

  def always_bad(video_id, dest, cfg, sink=None):  # noqa: ARG001
    raise QualityError("still below the floor")

  import pytest as _pytest

  with _pytest.MonkeyPatch.context() as m:
    m.setattr(pipeline, "download", always_bad)
    m.setattr(pipeline, "refresh_cookies", lambda: refreshed.append(True))
    for _ in range(5):
      with _pytest.raises(QualityError):
        pipeline._download_with_fresh_cookies(
          VideoRef(video_id="x", title="T", channel="c", duration_s=1),
          Config(),
          None,
          pipeline.LogBuffer(),
          state,
        )
  assert len(refreshed) == 1, f"refreshed {len(refreshed)} times across one run"
