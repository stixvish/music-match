"""Tests for the mutagen read/write wrapper.

Both container formats are exercised on real files: MP4 atoms via the
embedded M4A fixture, ID3 frames via a WAV built by the stdlib.
"""

import pathlib

import mutagen
import pytest

from music_match.tagging import fields
from music_match.tagging import tags as tag_io

FULL = fields.TrackTags(
    title="Strobe",
    artist="deadmau5",
    original_artist="deadmau5",
    composer="Joel Zimmerman",
    lyricist="Joel Zimmerman",
    remixer="Dimension",
    mix_name="Dimension Remix",
    album="For Lack of a Better Name",
    album_artist="deadmau5",
    genre="Progressive House",
    isrc="GBCEN0900123",
    release_date="2009-09-22",
    source_video_id="tKi9Z-f6qX4",
    year=2009,
    track_number=9,
    track_total=12,
    disc_number=1,
    disc_total=2,
)


@pytest.fixture(name="audio_file", params=["m4a_file", "wav_file"])
def fixture_audio_file(request: pytest.FixtureRequest) -> pathlib.Path:
  """Yields each supported container in turn.

  Args:
    request: The pytest request, carrying the fixture name to resolve.

  Returns:
    Path to an untagged audio file.
  """
  path = request.getfixturevalue(request.param)
  assert isinstance(path, pathlib.Path)
  return path


def test_untagged_file_reads_as_empty(audio_file: pathlib.Path) -> None:
  """A file with no tags reads back with every field unset."""
  assert tag_io.read_tags(audio_file).is_empty()


def test_round_trip_preserves_every_field(audio_file: pathlib.Path) -> None:
  """Everything written to a file comes back identically."""
  tag_io.write_tags(audio_file, FULL)
  assert tag_io.read_tags(audio_file) == FULL


def test_write_reports_the_changes_it_made(audio_file: pathlib.Path) -> None:
  """The returned diff names each field and its old and new value."""
  changes = tag_io.write_tags(audio_file,
                              fields.TrackTags(title="Strobe", year=2009))
  assert changes == {"title": (None, "Strobe"), "year": (None, 2009)}


def test_rewriting_the_same_values_is_a_no_op(audio_file: pathlib.Path) -> None:
  """A second identical write reports nothing changed."""
  tag_io.write_tags(audio_file, FULL)
  assert not tag_io.write_tags(audio_file, FULL)


def test_dry_run_reports_changes_without_writing(
    audio_file: pathlib.Path) -> None:
  """Dry run computes the same diff but leaves the file untouched."""
  before = audio_file.read_bytes()
  changes = tag_io.write_tags(audio_file, FULL, dry_run=True)
  assert changes
  assert audio_file.read_bytes() == before
  assert tag_io.read_tags(audio_file).is_empty()


def test_unset_fields_do_not_clear_existing_values(
    audio_file: pathlib.Path) -> None:
  """Writing a partial update leaves fields it says nothing about alone."""
  tag_io.write_tags(audio_file, FULL)
  changes = tag_io.write_tags(audio_file, fields.TrackTags(genre="Deep House"))
  after = tag_io.read_tags(audio_file)
  assert changes == {"genre": ("Progressive House", "Deep House")}
  assert after.genre == "Deep House"
  assert after.artist == "deadmau5"
  assert after.isrc == "GBCEN0900123"


def test_year_only_release_leaves_release_date_unset(
    audio_file: pathlib.Path) -> None:
  """A bare year is stored as a year, not invented into a full date."""
  tag_io.write_tags(audio_file, fields.TrackTags(year=2009))
  after = tag_io.read_tags(audio_file)
  assert after.year == 2009
  assert after.release_date is None


def test_track_number_without_total(audio_file: pathlib.Path) -> None:
  """A track number with no known total round-trips without a fake total."""
  tag_io.write_tags(audio_file, fields.TrackTags(track_number=3))
  after = tag_io.read_tags(audio_file)
  assert after.track_number == 3
  assert after.track_total is None


def test_source_video_id_is_not_a_visible_tag(m4a_file: pathlib.Path) -> None:
  """Internal bookkeeping stays out of the atoms a player displays."""
  before = set(mutagen.File(m4a_file).tags or {})
  tag_io.write_tags(m4a_file, fields.TrackTags(source_video_id="tKi9Z-f6qX4"))
  added = set(mutagen.File(m4a_file).tags) - before

  assert tag_io.read_tags(m4a_file).source_video_id == "tKi9Z-f6qX4"
  assert added == {"----:com.music-match:SOURCE_VIDEO_ID"}


def test_unicode_survives_the_round_trip(audio_file: pathlib.Path) -> None:
  """Non-ASCII metadata is not mangled by either container."""
  written = fields.TrackTags(artist="Röyksopp", title="Eple — Original Mix")
  tag_io.write_tags(audio_file, written)
  after = tag_io.read_tags(audio_file)
  assert after.artist == "Röyksopp"
  assert after.title == "Eple — Original Mix"


def test_read_rejects_a_non_audio_file(tmp_path: pathlib.Path) -> None:
  """A file mutagen cannot identify raises TagError, not a bare exception."""
  path = tmp_path / "notes.txt"
  path.write_text("not audio", encoding="utf-8")
  with pytest.raises(tag_io.TagError, match="unrecognized audio format"):
    tag_io.read_tags(path)


def test_read_rejects_a_missing_file(tmp_path: pathlib.Path) -> None:
  """A missing file names the path it could not read."""
  with pytest.raises(tag_io.TagError):
    tag_io.read_tags(tmp_path / "gone.m4a")


def test_merged_with_prefers_the_receiver() -> None:
  """Merging fills gaps without overwriting values already decided."""
  primary = fields.TrackTags(title="Strobe", artist="deadmau5")
  secondary = fields.TrackTags(title="Strobe (Radio Edit)", album="For Lack")
  merged = primary.merged_with(secondary)
  assert merged.title == "Strobe"
  assert merged.album == "For Lack"


def test_changes_against_ignores_unset_fields() -> None:
  """Fields this object says nothing about are never reported as changes."""
  current = fields.TrackTags(title="Old", genre="Music")
  update = fields.TrackTags(title="New")
  assert update.changes_against(current) == {"title": ("Old", "New")}


def test_as_dict_hides_unset_fields_by_default() -> None:
  """as_dict omits None unless asked for the full field list."""
  tags = fields.TrackTags(title="Strobe")
  assert tags.as_dict() == {"title": "Strobe"}
  assert set(tags.as_dict(include_empty=True)) == set(fields.ALL_FIELDS)


def test_year_overrides_a_contradicting_release_date(
    audio_file: pathlib.Path) -> None:
  """Writing a bare year drops a stale release date instead of losing it.

  `year` and `release_date` share one underlying field, so a merge that
  kept the old date would report a year change it never actually wrote.
  """
  tag_io.write_tags(audio_file, fields.TrackTags(release_date="2009-09-22"))
  changes = tag_io.write_tags(audio_file, fields.TrackTags(year=2010))
  after = tag_io.read_tags(audio_file)

  assert changes == {
      "year": (2009, 2010),
      "release_date": ("2009-09-22", None),
  }
  assert after.year == 2010
  assert after.release_date is None


def test_release_date_updates_the_year_it_implies(
    audio_file: pathlib.Path) -> None:
  """A new release date carries its year, and reports that as a change."""
  tag_io.write_tags(audio_file, fields.TrackTags(year=2009))
  changes = tag_io.write_tags(audio_file,
                              fields.TrackTags(release_date="2010-05-01"))
  after = tag_io.read_tags(audio_file)

  assert changes == {
      "release_date": (None, "2010-05-01"),
      "year": (2009, 2010),
  }
  assert after.year == 2010
  assert after.release_date == "2010-05-01"


def test_matching_year_leaves_the_release_date_alone(
    audio_file: pathlib.Path) -> None:
  """A year that agrees with the existing date is not a reason to drop it."""
  tag_io.write_tags(audio_file, fields.TrackTags(release_date="2009-09-22"))
  changes = tag_io.write_tags(audio_file, fields.TrackTags(year=2009))
  assert not changes
  assert tag_io.read_tags(audio_file).release_date == "2009-09-22"


def test_reported_changes_match_the_file(audio_file: pathlib.Path) -> None:
  """Every reported change is actually true of the file afterwards.

  This is the invariant `tag_history` depends on: a change logged but not
  written would make `undo` restore a value that never existed.
  """
  tag_io.write_tags(audio_file, FULL)
  update = fields.TrackTags(year=2011, genre="Deep House", track_number=4)
  changes = tag_io.write_tags(audio_file, update)

  after = tag_io.read_tags(audio_file).as_dict(include_empty=True)
  before = FULL.as_dict(include_empty=True)
  for field, (old_value, new_value) in changes.items():
    assert before[field] == old_value
    assert after[field] == new_value
