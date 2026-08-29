"""Tests for fingerprint encoding, parsing, and comparison.

Nothing here shells out to `fpcalc`. The subprocess boundary is tested by
feeding `_parse_fpcalc_json` the output shapes fpcalc actually produces,
and the comparison is tested on synthetic fingerprints whose expected
score can be reasoned about directly.
"""

import pathlib

import pytest

from music_match.tagging import fingerprint as fp


def make(values: tuple[int, ...],
         duration: float = 180.0,
         algorithm: int = fp.DEFAULT_ALGORITHM) -> fp.Fingerprint:
  """Builds a fingerprint without touching a file.

  Args:
    values: The raw sub-fingerprints.
    duration: The file duration to record.
    algorithm: The chromaprint algorithm to record.

  Returns:
    The fingerprint.
  """
  return fp.Fingerprint(values=values, duration=duration, algorithm=algorithm)


def test_encode_round_trips() -> None:
  """A stored fingerprint decodes back to exactly what was encoded."""
  original = make((0, 1, 2**31, 2**32 - 1, 12345))
  assert fp.decode(original.encode(), original.duration) == original


def test_encode_carries_the_algorithm() -> None:
  """The algorithm travels with the fingerprint, not in a second column."""
  encoded = make((1, 2, 3), algorithm=1).encode()
  assert encoded.startswith("1:")
  assert fp.decode(encoded, 180.0).algorithm == 1


def test_encode_is_compact() -> None:
  """Encoding stays near four bytes per value, not ten."""
  encoded = make(tuple(range(1000))).encode()
  assert len(encoded) < 1000 * 6


def test_decode_rejects_text_without_an_algorithm() -> None:
  """Text missing the prefix is refused rather than guessed at."""
  with pytest.raises(fp.FingerprintError, match="no algorithm prefix"):
    fp.decode("bm90YmFzZTY0", 180.0)


@pytest.mark.parametrize("text", ["2:not base64!", "x:AAAA"])
def test_decode_rejects_corrupt_text(text: str) -> None:
  """Corrupt stored fingerprints raise rather than returning nonsense."""
  with pytest.raises(fp.FingerprintError):
    fp.decode(text, 180.0)


def test_identical_fingerprints_score_one() -> None:
  """A file compared against itself is a perfect match."""
  fingerprint = make(tuple(range(100, 400)))
  assert fp.similarity(fingerprint, fingerprint) == 1.0


def test_unrelated_fingerprints_score_low() -> None:
  """Values with no bitwise relationship do not match."""
  first = make(tuple(0x55555555 ^ (index << 8) for index in range(300)))
  second = make(tuple(0xAAAAAAAA ^ (index << 8) for index in range(300)))
  assert fp.similarity(first, second) < 0.1


def test_small_bit_errors_still_match() -> None:
  """A re-encode perturbs a bit or two per value and must still match.

  This is the case that matters: a lossy transcode of the same audio
  produces nearly, but not exactly, the same sub-fingerprints.
  """
  base = tuple(index * 7919 for index in range(300))
  noisy = tuple(value ^ 0b11 for value in base)
  assert fp.similarity(make(base), make(noisy)) == 1.0


def test_large_bit_errors_do_not_match() -> None:
  """Differences beyond the bit-error budget are not the same audio."""
  base = tuple(index * 7919 for index in range(300))
  noisy = tuple(value ^ 0b11111111 for value in base)
  assert fp.similarity(make(base), make(noisy)) < 0.1


def test_offset_recordings_still_match() -> None:
  """A copy that starts a few seconds later still aligns.

  The comparison slides one fingerprint against the other, so a leading
  silence trimmed from one copy must not stop it matching.
  """
  base = tuple(index * 7919 for index in range(400))
  shifted = base[30:]
  assert fp.similarity(make(base), make(shifted)) == 1.0


def test_similarity_is_symmetric() -> None:
  """Argument order does not change the score."""
  earlier = make(tuple(index * 31 for index in range(200)))
  later = make(tuple(index * 31 for index in range(50, 250)))
  assert fp.similarity(earlier, later) == fp.similarity(later, earlier)


def test_empty_fingerprints_score_zero() -> None:
  """An empty fingerprint matches nothing, rather than dividing by zero."""
  assert fp.similarity(make(()), make((1, 2, 3))) == 0.0
  assert fp.similarity(make(()), make(())) == 0.0


def test_different_algorithms_are_refused() -> None:
  """Fingerprints from different algorithms are not comparable."""
  with pytest.raises(fp.FingerprintError, match="cannot compare"):
    fp.similarity(make((1, 2, 3), algorithm=1), make((1, 2, 3), algorithm=2))


def test_parse_fpcalc_output() -> None:
  """The JSON fpcalc -raw -json prints is parsed into a fingerprint."""
  payload = '{"duration": 172.70, "fingerprint": [1, 2, 4294967295]}'
  parsed = fp.parse_fpcalc_output(payload, pathlib.Path("x.m4a"), 2)
  assert parsed.values == (1, 2, 4294967295)
  assert parsed.duration == pytest.approx(172.70)
  assert parsed.algorithm == 2


@pytest.mark.parametrize("payload", [
    "",
    "not json",
    '{"duration": 1.0}',
    '{"fingerprint": [1, 2]}',
    '{"duration": "x", "fingerprint": [1]}',
])
def test_parse_rejects_unusable_output(payload: str) -> None:
  """Output that is not the expected shape raises FingerprintError."""
  with pytest.raises(fp.FingerprintError):
    fp.parse_fpcalc_output(payload, pathlib.Path("x.m4a"), 2)


def test_parse_rejects_a_file_with_no_audio() -> None:
  """A file fpcalc decoded but found nothing in is an error, not empty."""
  payload = '{"duration": 0.0, "fingerprint": []}'
  with pytest.raises(fp.FingerprintError, match="no audio"):
    fp.parse_fpcalc_output(payload, pathlib.Path("x.m4a"), 2)


def test_shared_values_counts_the_intersection() -> None:
  """The candidate filter counts sub-fingerprints common to both."""
  assert fp.shared_values([(1, 2, 3, 4), (3, 4, 5)]) == 2
  assert fp.shared_values([(1, 2), (3, 4)]) == 0
  assert fp.shared_values([]) == 0
