"""Art-track vs video-rip classification (tasks/todo.md t17, SPEC.md §8)."""

import pytest

from music.acquire.classify_source import (
  Classification,
  SourceSignals,
  Verdict,
  classify,
)


def test_topic_channel_is_an_art_track():
  result = classify(SourceSignals(channel="Burnie - Topic", title="Make It"))
  assert result.verdict is Verdict.ART_TRACK
  assert "topic_channel" in result.reasons


def test_provided_to_youtube_by_is_an_art_track():
  result = classify(
    SourceSignals(
      channel="Some Channel", description="Provided to YouTube by Amp Suite"
    )
  )
  assert result.verdict is Verdict.ART_TRACK


def test_art_track_evidence_beats_title_noise():
  """A Topic upload is label audio no matter what the title says."""
  result = classify(
    SourceSignals(
      channel="Artist - Topic",
      title="Song (Official Music Video)",
      duration_s=400.0,
      catalog_duration_s=200.0,
    )
  )
  assert result.verdict is Verdict.ART_TRACK


@pytest.mark.parametrize(
  "title",
  [
    "Nina Sky - Move Ya Body (Official Music Video) ft. Jabba",
    "Song [Official Video]",
    "Artist - Song (Lyric Video)",
    "Song M/V",
    "Song - Live at Wembley",
    "Song (Visualizer)",
  ],
)
def test_video_words_in_title(title):
  assert classify(SourceSignals(title=title)).verdict is Verdict.VIDEO_RIP


def test_artist_equal_to_channel_alone_does_not_convict():
  """Weak signal only.

  An artist tag equal to the channel describes an official artist-channel
  upload just as well as a rip. On its own it flagged all 2,329 library files,
  so it is recorded as a reason but never decides (SPEC.md §8).
  """
  result = classify(SourceSignals(channel="NINA_SKY", artist_tag="NINA_SKY", title="x"))
  assert result.verdict is Verdict.UNKNOWN
  assert "artist_is_channel" in result.reasons


def test_weak_signal_accompanies_a_strong_one():
  result = classify(
    SourceSignals(
      channel="NINA_SKY",
      artist_tag="NINA_SKY",
      title="Nina Sky - Move Ya Body (Official Music Video)",
    )
  )
  assert result.verdict is Verdict.VIDEO_RIP
  assert set(result.reasons) == {"video_in_title", "artist_is_channel"}


def test_longer_than_catalogue_is_a_rip():
  """Katy Perry's 'Last Friday Night' video runs 8.1 min for a 3.7 min song."""
  result = classify(
    SourceSignals(title="Last Friday Night", duration_s=486.0, catalog_duration_s=231.0)
  )
  assert result.verdict is Verdict.VIDEO_RIP
  assert "longer_than_catalogue" in result.reasons


def test_small_duration_difference_is_not_a_rip():
  assert (
    classify(
      SourceSignals(title="Song", duration_s=203.0, catalog_duration_s=200.0)
    ).verdict
    is Verdict.UNKNOWN
  )


def test_shorter_than_catalogue_is_not_flagged():
  """A radio edit is shorter, not longer; that is a version question, not a rip."""
  result = classify(
    SourceSignals(title="Song", duration_s=180.0, catalog_duration_s=240.0)
  )
  assert result.verdict is Verdict.UNKNOWN


def test_plain_track_is_unknown_not_guessed():
  assert classify(SourceSignals(channel="Some Channel", title="Song")).verdict is (
    Verdict.UNKNOWN
  )


def test_multiple_reasons_are_all_reported():
  result = classify(
    SourceSignals(
      channel="VEVO",
      artist_tag="VEVO",
      title="Song (Official Video)",
      duration_s=400.0,
      catalog_duration_s=200.0,
    )
  )
  assert set(result.reasons) == {
    "video_in_title",
    "artist_is_channel",
    "longer_than_catalogue",
  }


def test_is_video_rip_helper():
  assert Classification(Verdict.VIDEO_RIP).is_video_rip
  assert not Classification(Verdict.ART_TRACK).is_video_rip
  assert not Classification(Verdict.UNKNOWN).is_video_rip
