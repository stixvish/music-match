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
