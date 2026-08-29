"""Tests for duplicate detection and keeper selection."""

import pathlib

import pytest

from music_match.tagging import dedup
from music_match.tagging import fingerprint as fp
from music_match.tagging import quality

# A base fingerprint long enough to clear MIN_SHARED_SUBFINGERPRINTS.
BASE = tuple(index * 7919 for index in range(300))


def track(track_id: int,
          path: str,
          values: tuple[int, ...] = BASE,
          *,
          duration: float = 180.0,
          lossless: bool = False,
          bitrate: int = 128_000) -> dedup.IndexedTrack:
  """Builds an IndexedTrack without touching a file.

  Args:
    track_id: The row id to record.
    path: The file path to record.
    values: Its raw sub-fingerprints.
    duration: Its duration in seconds.
    lossless: Whether its audio is lossless.
    bitrate: Its bitrate.

  Returns:
    The constructed track.
  """
  return dedup.IndexedTrack(track_id=track_id,
                            path=pathlib.Path(path),
                            fingerprint=fp.Fingerprint(values=values,
                                                       duration=duration),
                            quality=quality.AudioQuality(lossless=lossless,
                                                         bitrate=bitrate,
                                                         sample_rate=44100,
                                                         bits_per_sample=16,
                                                         codec="test"))


def test_no_duplicates_produces_no_groups() -> None:
  """Unrelated tracks are left alone."""
  others = tuple(0xAAAAAAAA ^ (index << 8) for index in range(300))
  assert not dedup.find_duplicates(
      [track(1, "/a.m4a"), track(2, "/b.m4a", others)])


def test_identical_tracks_group_together() -> None:
  """Two copies of the same recording form one group."""
  groups = dedup.find_duplicates([track(1, "/a.m4a"), track(2, "/b.m4a")])
  assert len(groups) == 1
  assert len(groups[0].duplicates) == 1


def test_lossless_copy_is_kept_over_lossy() -> None:
  """Format tier decides the keeper, whatever the scan order."""
  lossy = track(1, "/lossy.m4a", lossless=False, bitrate=320_000)
  lossless = track(2, "/lossless.wav", lossless=True, bitrate=141_000)
  groups = dedup.find_duplicates([lossy, lossless])
  assert groups[0].keeper.path.name == "lossless.wav"
  assert groups[0].duplicates[0].track.path.name == "lossy.m4a"


def test_keeper_does_not_depend_on_input_order() -> None:
  """Reversing the input picks the same keeper."""
  lossy = track(1, "/lossy.m4a", lossless=False, bitrate=320_000)
  lossless = track(2, "/lossless.wav", lossless=True, bitrate=141_000)
  forward = dedup.find_duplicates([lossy, lossless])
  backward = dedup.find_duplicates([lossless, lossy])
  assert forward[0].keeper.path == backward[0].keeper.path


def test_higher_bitrate_wins_within_a_tier() -> None:
  """Among lossy copies the higher bitrate is kept."""
  groups = dedup.find_duplicates([
      track(1, "/low.m4a", bitrate=128_000),
      track(2, "/high.m4a", bitrate=320_000),
  ])
  assert groups[0].keeper.path.name == "high.m4a"


def test_transcodes_still_group() -> None:
  """A lossy copy of the same audio still matches.

  Modelled on what a real transcode does: most sub-fingerprints come out
  identical and a minority shift by a bit or two. Measured on this
  library, a 128kbps transcode shared 718 of 916 values exactly.
  """
  noisy = tuple(value ^ 0b1 if index % 5 == 0 else value
                for index, value in enumerate(BASE))
  groups = dedup.find_duplicates(
      [track(1, "/a.m4a"), track(2, "/b.m4a", noisy)])
  assert len(groups) == 1
  assert groups[0].duplicates[0].similarity == pytest.approx(1.0)


def test_duration_gate_blocks_a_snippet_from_displacing_a_track() -> None:
  """A short excerpt must not count as a duplicate of the full recording.

  Similarity divides by the shorter fingerprint, so a snippet scores near
  1.0 against the track it came from. Only the duration gate stops a
  30-second clip from winning on bitrate and displacing the real file.
  """
  full = track(1, "/full.m4a", BASE, duration=180.0, bitrate=128_000)
  snippet = track(2, "/snippet.m4a", BASE[:60], duration=30.0, bitrate=320_000)
  assert not dedup.find_duplicates([full, snippet])


def test_duration_tolerance_is_configurable() -> None:
  """Widening the gate lets a longer edit through."""
  first = track(1, "/a.m4a", duration=180.0)
  second = track(2, "/b.m4a", duration=200.0)
  assert not dedup.find_duplicates([first, second])
  assert dedup.find_duplicates([first, second], duration_tolerance=60.0)


def test_threshold_is_configurable() -> None:
  """Raising the threshold past a pair's score splits them apart.

  Half these values are untouched, so the pair clears the candidate
  filter and scores about 0.5 — below the default threshold, above a
  lowered one.
  """
  noisy = tuple(value ^ 0b11111111 if index % 2 else value
                for index, value in enumerate(BASE))
  pair = [track(1, "/a.m4a"), track(2, "/b.m4a", noisy)]
  assert not dedup.find_duplicates(pair)
  assert dedup.find_duplicates(pair, threshold=0.4)


def test_candidate_filter_needs_exactly_shared_subfingerprints() -> None:
  """A known limitation, pinned down rather than left to be discovered.

  The candidate filter only considers pairs sharing at least
  MIN_SHARED_SUBFINGERPRINTS values *exactly*. A copy that perturbed
  every single sub-fingerprint would score 1.0 on a full comparison but
  is never compared at all. Real transcodes leave most values untouched
  — measured at 718 of 916 for 128kbps, and 417 for a two-second-shifted
  re-encode — so this is a wide safety margin in practice, not a
  near-miss.
  """
  perturbed = tuple(value ^ 0b1 for value in BASE)
  pair = [track(1, "/a.m4a"), track(2, "/b.m4a", perturbed)]
  assert fp.similarity(pair[0].fingerprint, pair[1].fingerprint) == 1.0
  assert not dedup.find_duplicates(pair)


def test_three_copies_form_one_group() -> None:
  """A recording held three times produces one group, not two."""
  groups = dedup.find_duplicates([
      track(1, "/a.m4a", bitrate=128_000),
      track(2, "/b.m4a", bitrate=192_000),
      track(3, "/c.m4a", bitrate=320_000),
  ])
  assert len(groups) == 1
  assert groups[0].keeper.path.name == "c.m4a"
  assert len(groups[0].duplicates) == 2


def test_different_algorithms_are_never_compared() -> None:
  """Fingerprints from different algorithms cannot be duplicates."""
  first = track(1, "/a.m4a")
  second = dedup.IndexedTrack(track_id=2,
                              path=pathlib.Path("/b.m4a"),
                              fingerprint=fp.Fingerprint(values=BASE,
                                                         duration=180.0,
                                                         algorithm=1),
                              quality=first.quality)
  assert not dedup.find_duplicates([first, second])


def test_destination_preserves_the_path_below_the_source(
    tmp_path: pathlib.Path) -> None:
  """Two duplicates with the same file name do not collide."""
  destination_root = tmp_path / "dupes"
  moved = track(1, str(tmp_path / "yt-dlp" / "Album" / "01 Track.m4a"))
  destination = dedup.destination_for(moved, "yt-dlp", destination_root)
  assert destination == destination_root / "Album" / "01 Track.m4a"


def test_destination_survives_a_moved_library(tmp_path: pathlib.Path) -> None:
  """The source folder is found by name, so a moved library still works."""
  moved = track(1, "/Volumes/External/Archive/yt-dlp/Album/Track.m4a")
  destination = dedup.destination_for(moved, "yt-dlp", tmp_path / "dupes")
  assert destination == tmp_path / "dupes" / "Album" / "Track.m4a"


def test_destination_never_overwrites(tmp_path: pathlib.Path) -> None:
  """An occupied destination gets a numbered name instead."""
  destination_root = tmp_path / "dupes"
  destination_root.mkdir()
  (destination_root / "Track.m4a").write_bytes(b"existing")
  moved = track(1, str(tmp_path / "yt-dlp" / "Track.m4a"))
  destination = dedup.destination_for(moved, "yt-dlp", destination_root)
  assert destination.name == "Track (1).m4a"
  assert not destination.exists()


def test_destination_falls_back_for_a_path_outside_the_source(
    tmp_path: pathlib.Path) -> None:
  """A file not under the source root still gets a usable destination."""
  moved = track(1, "/elsewhere/Track.m4a")
  destination = dedup.destination_for(moved, "yt-dlp", tmp_path / "dupes")
  assert destination == tmp_path / "dupes" / "Track.m4a"
