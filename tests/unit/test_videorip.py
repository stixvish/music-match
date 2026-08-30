"""Tests for spotting audio ripped from a music video.

The marker lists come from this library's own filenames: of 2412 files,
112 carry a video marker, and every file whose name mentions "video" at
all matches one of the phrases — no ordinary title was caught by
accident.
"""

import pathlib

import pytest

from music_match.matching import normalize as norm
from music_match.tagging import videorip


@pytest.mark.parametrize("stem", [
    "Ariana Grande - 7 rings (Official Video)",
    "Cardi_B_-_Bodak_Yellow_OFFICIAL_MUSIC_VIDEO",
    "Beyonce_-_Run_the_World_Girls_Official_Video",
    "Some Artist - Some Song Music Video",
])
def test_video_markers_are_detected(stem: str) -> None:
  """The phrases that actually appear in this library are caught."""
  assert videorip.detect(pathlib.Path(f"{stem}.m4a")).is_rip


@pytest.mark.parametrize("stem", [
    "Sia_-_Cheap_Thrills_Official_Lyric_Video_ft._Sean_Paul",
    "Morgan_Wallen_-_What_I_Want_Lyric_Video",
    "Shaboozey_-_A_Bar_Song_Tipsy_Official_Visualizer",
    "Some Artist - Some Song Official Audio",
    "Ghost_Town_DJ_s_-_My_Boo_Lyrics",
])
def test_audio_only_uploads_are_not_rips(stem: str) -> None:
  """A lyric video or visualiser carries the studio audio.

  The picture is decoration and the file is exactly what it should be.
  Flagging these would put a tenth of the library in a queue for nothing.
  """
  assert not videorip.detect(pathlib.Path(f"{stem}.m4a")).is_rip


@pytest.mark.parametrize("stem", [
    "Lana Del Rey - Video Games",
    "The Buggles - Video Killed the Radio Star",
    "Some Artist - Videotape",
])
def test_ordinary_titles_are_not_flagged(stem: str) -> None:
  """"Video" in a song title is not a marker; the phrases are."""
  assert not videorip.detect(pathlib.Path(f"{stem}.m4a")).is_rip


def test_the_embedded_title_is_checked_too() -> None:
  """A tidy filename can still hide a video rip in its tags."""
  detection = videorip.detect(pathlib.Path("track.m4a"),
                              "Bodak Yellow (Official Music Video)")
  assert detection.is_rip
  assert detection.where == ("title",)


def test_the_filename_is_checked_too() -> None:
  """And the reverse: tidy tags, telling filename."""
  detection = videorip.detect(pathlib.Path("Artist - Song Official Video.m4a"),
                              "Song")
  assert detection.where == ("filename",)


def test_overlapping_markers_are_reported_once() -> None:
  """"official music video" contains "music video"; say it once."""
  detection = videorip.detect(
      pathlib.Path("Artist - Song Official Music Video.m4a"))
  assert detection.markers == ("official music video",)


def test_detection_does_not_use_the_matching_normaliser() -> None:
  """A real bug this guards: the two normalisers want opposite things.

  `matching.normalize` strips "official video" as upload noise, which is
  correct for comparing titles and fatal for detecting them — running
  detection through it removes every marker before the search begins.
  """
  stem = "Artist - Song Official Video"
  assert "official video" not in norm.normalize(stem)
  assert "official video" in videorip.flatten(stem)
  assert videorip.detect(pathlib.Path(f"{stem}.m4a")).is_rip


def test_a_clean_file_reports_no_markers() -> None:
  """Nothing found is stated plainly rather than left blank."""
  detection = videorip.detect(pathlib.Path("Adele - Hello.m4a"))
  assert not detection.is_rip
  assert detection.describe() == "no video markers"


def test_markers_survive_underscored_filenames() -> None:
  """yt-dlp writes underscores where the upload had spaces."""
  assert videorip.markers_in("Artist_-_Song_Official_Video") == (
      "official video",)


def test_vevo_is_treated_as_a_video_source() -> None:
  """That platform hosts nothing but videos."""
  assert videorip.detect(pathlib.Path("ArtistVEVO - Song.m4a")).is_rip
