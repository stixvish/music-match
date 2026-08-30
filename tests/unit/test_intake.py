"""Tests for link expansion and the two pre-download dedup layers.

Nothing here reaches the network: yt-dlp's own output shapes are fed to
the parsing functions directly, which is the same boundary the source
adapters are tested at.
"""

import pathlib
import sqlite3

import pytest

from music_match.db import connection
from music_match.db import queries
from music_match.intake import dedup
from music_match.intake import download as download_lib
from music_match.intake import entries as entries_lib

# ------------------------------------------------------------ link input


def test_parse_links_reads_one_per_line() -> None:
  """A pasted block of links is the normal submission."""
  assert entries_lib.parse_links("a\nb\nc") == ["a", "b", "c"]


def test_parse_links_ignores_blanks_and_comments() -> None:
  """A file of links should be annotatable."""
  text = "# my playlist\n\nhttps://x/1\n  \nhttps://x/2  # a note\n"
  assert entries_lib.parse_links(text) == ["https://x/1", "https://x/2"]


def test_parse_links_deduplicates() -> None:
  """The same link pasted twice is one submission."""
  assert entries_lib.parse_links("a\nb\na") == ["a", "b"]


# ------------------------------------------------------------- expansion


def test_a_single_video_yields_one_entry() -> None:
  """The shape yt-dlp returns for a plain video link."""
  info = {
      "id": "abc123",
      "extractor": "youtube",
      "title": "Strobe",
      "uploader": "deadmau5",
      "duration": 636,
      "webpage_url": "https://x/abc123",
  }
  found = entries_lib.entries_from_info(info)
  assert len(found) == 1
  assert found[0].video_id == "abc123"
  assert found[0].duration_seconds == 636.0
  assert found[0].label() == "deadmau5 - Strobe"


def test_a_playlist_is_flattened() -> None:
  """Albums and playlists are expanded into their members."""
  info = {
      "_type":
          "playlist",
      "title":
          "A playlist",
      "entries": [
          {
              "id": "one",
              "ie_key": "Youtube",
              "title": "First"
          },
          {
              "id": "two",
              "ie_key": "Youtube",
              "title": "Second"
          },
      ],
  }
  found = entries_lib.entries_from_info(info, "https://x/list")
  assert [entry.video_id for entry in found] == ["one", "two"]


def test_a_flat_entry_names_its_extractor_differently() -> None:
  """A flat playlist entry has `ie_key`; a resolved video `extractor`."""
  flat = entries_lib.entries_from_info({
      "_type": "playlist",
      "entries": [{
          "id": "one",
          "ie_key": "Youtube"
      }],
  })
  assert flat[0].extractor == "youtube"


def test_nested_playlists_are_flattened() -> None:
  """A channel link can return playlists of playlists."""
  info = {
      "_type":
          "playlist",
      "entries": [{
          "_type": "playlist",
          "entries": [{
              "id": "deep",
              "ie_key": "Youtube"
          }],
      }],
  }
  assert [e.video_id for e in entries_lib.entries_from_info(info)] == ["deep"]


def test_an_entry_without_an_id_is_dropped() -> None:
  """A listing can carry placeholder rows for unavailable videos."""
  assert not entries_lib.entries_from_info({"title": "no id here"})


def test_label_falls_back_when_untitled() -> None:
  """Something readable is always available for the progress line."""
  entry = entries_lib.Entry(video_id="abc", extractor="youtube")
  assert entry.label() == "abc"


# --------------------------------------------------- layer 1, the archive


@pytest.fixture(name="conn")
def fixture_conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
  """Opens an initialized database.

  Args:
    tmp_path: pytest's per-test temporary directory.

  Returns:
    The connection.
  """
  connection_ = connection.connect(tmp_path / "state.db")
  connection.initialize(connection_)
  return connection_


ENTRY = entries_lib.Entry(video_id="abc123",
                          extractor="youtube",
                          title="Strobe",
                          uploader="deadmau5",
                          duration_seconds=636.0,
                          url="https://x/abc123")


def test_an_unknown_entry_is_not_in_the_archive(
    conn: sqlite3.Connection) -> None:
  """A fresh link has never been downloaded."""
  assert not dedup.in_archive(conn, ENTRY)


def test_a_recorded_entry_is_in_the_archive(conn: sqlite3.Connection) -> None:
  """Recording a download is what makes the check instant next time."""
  dedup.record_download(conn, ENTRY)
  assert dedup.in_archive(conn, ENTRY)


def test_the_archive_is_keyed_per_extractor(conn: sqlite3.Connection) -> None:
  """The same id on two sites is two different tracks."""
  dedup.record_download(conn, ENTRY)
  elsewhere = entries_lib.Entry(video_id="abc123", extractor="soundcloud")
  assert not dedup.in_archive(conn, elsewhere)


def test_recording_twice_is_harmless(conn: sqlite3.Connection) -> None:
  """A retried run must not fail on its own earlier record."""
  dedup.record_download(conn, ENTRY)
  dedup.record_download(conn, ENTRY, track_id=None)
  assert dedup.in_archive(conn, ENTRY)


# ------------------------------------------------- layer 2, the pre-check


def index(conn: sqlite3.Connection, name: str, duration: float) -> None:
  """Adds a library file to the index.

  Args:
    conn: An open connection.
    name: The file name.
    duration: Its duration in seconds.
  """
  queries.upsert_track(conn,
                       path=pathlib.Path("/music/yt-dlp") / name,
                       source_name="yt-dlp",
                       duration_seconds=duration)


def test_a_similar_title_and_length_raises_a_candidate(
    conn: sqlite3.Connection) -> None:
  """The pre-check exists to notice this before spending a download."""
  index(conn, "deadmau5 - Strobe.m4a", 636.0)
  candidates = dedup.find_candidates(conn, ENTRY)
  assert candidates
  assert candidates[0].path.name == "deadmau5 - Strobe.m4a"


def test_a_different_length_is_not_a_candidate(
    conn: sqlite3.Connection) -> None:
  """A radio edit and a ten-minute original are not the same file."""
  index(conn, "deadmau5 - Strobe.m4a", 200.0)
  assert not dedup.find_candidates(conn, ENTRY)


def test_a_different_title_is_not_a_candidate(conn: sqlite3.Connection) -> None:
  """Matching on length alone would flag every track of similar length."""
  index(conn, "deadmau5 - Ghosts n Stuff.m4a", 636.0)
  assert not dedup.find_candidates(conn, ENTRY)


def test_an_untitled_entry_raises_nothing(conn: sqlite3.Connection) -> None:
  """With no title there is nothing to compare."""
  index(conn, "deadmau5 - Strobe.m4a", 636.0)
  untitled = entries_lib.Entry(video_id="x", extractor="youtube")
  assert not dedup.find_candidates(conn, untitled)


def test_an_entry_of_unknown_length_still_compares_titles(
    conn: sqlite3.Connection) -> None:
  """A missing duration weakens the check; it does not disable it."""
  index(conn, "deadmau5 - Strobe.m4a", 636.0)
  no_length = entries_lib.Entry(video_id="x",
                                extractor="youtube",
                                title="Strobe")
  assert dedup.find_candidates(conn, no_length)


def test_candidates_are_ordered_by_closeness(conn: sqlite3.Connection) -> None:
  """The best guess goes first in the prompt."""
  index(conn, "deadmau5 - Strobe.m4a", 636.0)
  index(conn, "deadmau5 - Strobe (Club Edit).m4a", 636.0)
  candidates = dedup.find_candidates(conn, ENTRY)
  assert candidates[0].similarity >= candidates[-1].similarity


def test_candidate_describes_itself_readably() -> None:
  """The prompt has to be answerable at a glance."""
  candidate = dedup.Candidate(path=pathlib.Path("/m/Strobe.m4a"),
                              title="Strobe",
                              duration_seconds=636.0,
                              similarity=0.9)
  described = candidate.describe()
  assert "Strobe.m4a" in described
  assert "10:36" in described


# -------------------------------------------------------------- download


def test_format_prefers_m4a_without_transcoding() -> None:
  """The library is M4A; taking that container avoids re-encoding.

  It also avoids depending on ffmpeg being installed.
  """
  assert download_lib.FORMAT_SELECTOR.startswith("bestaudio[ext=m4a]")


def test_output_template_matches_the_library_convention() -> None:
  """"Uploader - Title" is what the matcher falls back to when untagged."""
  assert download_lib.OUTPUT_TEMPLATE.startswith("%(uploader)s - %(title)s")


def test_progress_output_is_suppressed(tmp_path: pathlib.Path) -> None:
  """`quiet` does not cover the progress bar, which redraws constantly."""
  options = download_lib.options_for(tmp_path)
  assert options["noprogress"] is True
  assert options["quiet"] is True


def test_downloaded_path_reads_the_modern_field(tmp_path: pathlib.Path) -> None:
  """yt-dlp reports the written file under `requested_downloads`."""
  info = {"requested_downloads": [{"filepath": str(tmp_path / "a.m4a")}]}
  assert download_lib.downloaded_path(info) == tmp_path / "a.m4a"


def test_downloaded_path_falls_back(tmp_path: pathlib.Path) -> None:
  """Older shapes name the file at the top level."""
  info = {"filepath": str(tmp_path / "a.m4a")}
  assert download_lib.downloaded_path(info) == tmp_path / "a.m4a"


def test_downloaded_path_of_nothing_is_none() -> None:
  """A result naming no file is reported rather than guessed at."""
  assert download_lib.downloaded_path({}) is None


def test_source_id_is_stamped_into_the_file(m4a_file: pathlib.Path) -> None:
  """This is what lets reindex rebuild the archive from files alone."""
  from music_match.tagging import tags as tag_io  # pylint: disable=import-outside-toplevel
  assert download_lib.stamp_source_id(m4a_file, ENTRY)
  assert tag_io.read_tags(m4a_file).source_video_id == "abc123"


def test_stamping_an_unreadable_file_is_reported_not_raised(
    tmp_path: pathlib.Path) -> None:
  """The download succeeded; a tagging failure must not undo that."""
  path = tmp_path / "notes.txt"
  path.write_text("not audio", encoding="utf-8")
  assert not download_lib.stamp_source_id(path, ENTRY)
