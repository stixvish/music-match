"""Transcode and tag round-trip (tasks/todo.md t5, t25).

Guards the class of bug where a frame is silently dropped on write — TDRC was
lost exactly this way during the format probe (SPEC.md §10).
"""

from music.acquire import probe
from music.publish import tag, transcode

FULL_TAGS = {
  "title": "Delilah",
  "artist": "Fred again..",
  "album": "Actual Life 3",
  "album_artist": "Fred again..",
  "genre": "Electronic",
  "track_number": "7",
  "disc_number": "1",
  "bpm": "128",
  "composer": "A Composer",
  "lyricist": "A Lyricist",
  "remixer": "Tom Santa",
  "mix_name": "Tom Santa Remix",
  "label": "Atlantic",
  "original_artist": "Fred again..",
  "key": "8A",
  "isrc": "GBAHS2200123",
  "grouping": "peak time",
  "year": "2022",
  "release_date": "2022-10-28",
}


def test_transcode_produces_16bit_aiff(synth_audio, tmp_path):
  out = transcode.to_aiff(synth_audio, tmp_path / "out.aiff")
  info = probe.probe(out)
  assert info.codec == "pcm_s16be"
  assert info.sample_rate == 44100
  assert info.channels == 2


def test_every_frame_survives_a_round_trip(synth_audio, tmp_path):
  out = transcode.to_aiff(synth_audio, tmp_path / "rt.aiff")
  tags = tag.Tags()
  for key, value in FULL_TAGS.items():
    tags[key] = value
  tags.comment = "a comment"
  tag.write(out, tags)

  back = tag.read(out)
  for key, value in FULL_TAGS.items():
    assert back.values.get(key) == value, f"{key} did not survive"
  assert back.comment == "a comment"


def test_year_and_release_date_are_separate_frames(synth_audio, tmp_path):
  """Rekordbox shows TDRC as Year and TDRL as Release Date (SPEC.md §10)."""
  out = transcode.to_aiff(synth_audio, tmp_path / "d.aiff")
  tags = tag.Tags()
  tags["year"] = "2001"
  tags["release_date"] = "2004-04-04"
  tag.write(out, tags)

  back = tag.read(out)
  assert back["year"] == "2001"
  assert back["release_date"] == "2004-04-04"


def test_artwork_round_trips(synth_audio, tmp_path):
  out = transcode.to_aiff(synth_audio, tmp_path / "art.aiff")
  tags = tag.Tags()
  tags["title"] = "With Art"
  tags.artwork = b"\xff\xd8\xff\xe0" + b"\x00" * 128
  tag.write(out, tags)
  assert tag.read(out).artwork == tags.artwork


def test_unknown_field_is_rejected():
  tags = tag.Tags()
  try:
    tags["not_a_real_field"] = "x"
  except KeyError:
    return
  raise AssertionError("expected KeyError for an unmapped field")


# --- frames belonging to other tools must survive a rewrite ---------------


def test_serato_analysis_survives_a_retag(synth_audio, tmp_path):
  """Serato stores beatgrids and cue points in GEOB frames inside the ID3 tag.

  A naive delete-then-write destroys them. Verified on real library files that
  already carried Serato BeatGrid, Markers2, Autotags and Overview (cp6).
  """
  from mutagen.aiff import AIFF
  from mutagen.id3 import GEOB

  out = transcode.to_aiff(synth_audio, tmp_path / "serato.aiff")
  tags = tag.Tags()
  tags["title"] = "Original"
  tag.write(out, tags)

  audio = AIFF(str(out))
  audio.tags.add(
    GEOB(
      encoding=0,
      mime="application/octet-stream",
      desc="Serato BeatGrid",
      data=b"BEATGRID-DATA",
    )
  )
  audio.tags.add(
    GEOB(
      encoding=0,
      mime="application/octet-stream",
      desc="Serato Markers2",
      data=b"CUE-DATA",
    )
  )
  audio.save(v2_version=4)

  # a full rewrite, as `music retag` performs
  updated = tag.read(out)
  updated["title"] = "Changed"
  tag.write(out, updated)

  after = AIFF(str(out))
  assert "GEOB:Serato BeatGrid" in after.tags
  assert "GEOB:Serato Markers2" in after.tags
  assert after.tags["GEOB:Serato BeatGrid"].data == b"BEATGRID-DATA"
  assert tag.read(out)["title"] == "Changed"


def test_owned_frames_are_replaced_not_duplicated(synth_audio, tmp_path):
  out = transcode.to_aiff(synth_audio, tmp_path / "dup.aiff")
  first = tag.Tags()
  first["genre"] = "House"
  tag.write(out, first)
  second = tag.Tags()
  second["genre"] = "Techno"
  tag.write(out, second)
  assert tag.read(out)["genre"] == "Techno"


def test_unknown_third_party_frames_survive(synth_audio, tmp_path):
  """Anything we do not write belongs to another tool."""
  from mutagen.aiff import AIFF
  from mutagen.id3 import TXXX

  out = transcode.to_aiff(synth_audio, tmp_path / "third.aiff")
  tag.write(out, tag.Tags())
  audio = AIFF(str(out))
  audio.tags.add(TXXX(encoding=3, desc="SomeOtherTool", text=["keep me"]))
  audio.save(v2_version=4)

  tag.write(out, tag.Tags())
  assert AIFF(str(out)).tags["TXXX:SomeOtherTool"].text == ["keep me"]
