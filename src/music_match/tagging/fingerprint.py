"""Chromaprint audio fingerprinting.

Fingerprints are computed locally by shelling out to `fpcalc`, with no
network call, and are the basis of dedup layer 3 — the final safety net
for duplicates that the archive check and the metadata pre-check missed.

Two notes on why this shells out rather than using pyacoustid's
`fingerprint_file` and `compare_fingerprints`:

- Those need `libchromaprint` through ctypes, and
  `ctypes.util.find_library("chromaprint")` returns None inside this
  project's virtualenv even with the Homebrew library installed. The
  `fpcalc` binary is what SETUP.md already asks for and is reliably
  found on PATH.
- Comparing two fingerprints needs the *raw* (uncompressed) form, which
  `fpcalc -raw` prints directly. Getting there from the compressed form
  would need the same missing library to decompress it.

The compressed form is what the AcoustID API wants, so step 5 will need
`fpcalc` again without `-raw` for the minority of tracks that fall
through to a network lookup. Storing only the raw form here keeps the
one-time index over ~2000 files to a single decode per file.
"""

import base64
import dataclasses
import json
import pathlib
import shutil
import struct
import subprocess
from typing import Any, Iterable, Sequence

FPCALC = "fpcalc"

# Seconds of audio fingerprinted. fpcalc's own default; long enough to
# identify a track, short enough to keep a full-library scan quick.
DEFAULT_LENGTH_SECONDS = 120

# fpcalc's default algorithm. Recorded per fingerprint because
# fingerprints from different algorithms are not comparable.
DEFAULT_ALGORITHM = 2

# Two 32-bit sub-fingerprints this many differing bits apart or fewer
# count as the same moment of audio. Chromaprint's own comparison
# constants, which the AcoustID server also uses.
MAX_BIT_ERROR = 2
MAX_ALIGN_OFFSET = 120

_TIMEOUT_SECONDS = 120


class FingerprintError(Exception):
  """Raised when a file cannot be fingerprinted."""


@dataclasses.dataclass(frozen=True)
class Fingerprint:
  """A raw chromaprint fingerprint.

  Attributes:
    values: The uncompressed 32-bit sub-fingerprints, in order.
    duration: The full duration of the file in seconds, which is not the
      duration fingerprinted — `values` covers at most
      `DEFAULT_LENGTH_SECONDS`.
    algorithm: The chromaprint algorithm used. Fingerprints from
      different algorithms must never be compared.
  """
  values: tuple[int, ...]
  duration: float
  algorithm: int = DEFAULT_ALGORITHM

  def encode(self) -> str:
    """Packs the fingerprint for storage in a TEXT column.

    The algorithm is written into the text as an `<algorithm>:` prefix
    rather than a separate column, so a stored fingerprint always
    carries what is needed to know whether it may be compared. The rest
    is base64 of the packed little-endian uint32 array, roughly half the
    size of the comma-separated decimal form.

    Returns:
      The encoded fingerprint.
    """
    packed = struct.pack(f"<{len(self.values)}I", *self.values)
    body = base64.b64encode(packed).decode("ascii")
    return f"{self.algorithm}:{body}"

  def is_empty(self) -> bool:
    """Returns whether the fingerprint carries no sub-fingerprints."""
    return not self.values


def decode(text: str, duration: float) -> Fingerprint:
  """Rebuilds a fingerprint stored by `Fingerprint.encode`.

  Args:
    text: The `<algorithm>:<base64>` text produced by `encode`.
    duration: The file duration recorded alongside it.

  Returns:
    The decoded fingerprint.

  Raises:
    FingerprintError: If the text is not a fingerprint this module wrote.
  """
  prefix, separator, body = text.partition(":")
  if not separator:
    raise FingerprintError(
        "stored fingerprint has no algorithm prefix; re-run scan --force")
  try:
    algorithm = int(prefix)
    packed = base64.b64decode(body, validate=True)
    values = struct.unpack(f"<{len(packed) // 4}I", packed)
  except (ValueError, struct.error) as err:
    raise FingerprintError(f"corrupt stored fingerprint: {err}") from err
  return Fingerprint(values=values, duration=duration, algorithm=algorithm)


def have_fpcalc() -> bool:
  """Returns whether the `fpcalc` binary is on PATH."""
  return shutil.which(FPCALC) is not None


def fingerprint_file(
    path: pathlib.Path,
    *,
    length: int = DEFAULT_LENGTH_SECONDS,
    algorithm: int = DEFAULT_ALGORITHM,
) -> Fingerprint:
  """Fingerprints an audio file with `fpcalc`.

  Args:
    path: The audio file to fingerprint.
    length: Seconds of audio to fingerprint.
    algorithm: The chromaprint algorithm to use.

  Returns:
    The file's raw fingerprint.

  Raises:
    FingerprintError: If `fpcalc` is missing, fails, times out, or
      returns something this module cannot parse — including a file it
      decoded but found no audio in.
  """
  if not have_fpcalc():
    raise FingerprintError(
        "fpcalc not found on PATH; install chromaprint (see SETUP.md)")
  command = [
      FPCALC, "-raw", "-json", "-length",
      str(length), "-algorithm",
      str(algorithm),
      str(path)
  ]
  try:
    result = subprocess.run(command,
                            capture_output=True,
                            text=True,
                            check=True,
                            timeout=_TIMEOUT_SECONDS)
  except subprocess.TimeoutExpired as err:
    raise FingerprintError(f"fpcalc timed out on {path}") from err
  except subprocess.CalledProcessError as err:
    detail = (err.stderr or "").strip().splitlines()
    reason = detail[-1] if detail else f"exit status {err.returncode}"
    raise FingerprintError(f"fpcalc failed on {path}: {reason}") from err

  return parse_fpcalc_output(result.stdout, path, algorithm)


def parse_fpcalc_output(payload: str, path: pathlib.Path,
                        algorithm: int) -> Fingerprint:
  """Parses `fpcalc -raw -json` output.

  Args:
    payload: The JSON fpcalc printed on stdout.
    path: The file it described, used in error messages.
    algorithm: The algorithm fpcalc was asked for.

  Returns:
    The parsed fingerprint.

  Raises:
    FingerprintError: If the output is not the shape this module expects,
      or describes a file with no audio.
  """
  try:
    document: dict[str, Any] = json.loads(payload)
    raw_values = document["fingerprint"]
    duration = float(document["duration"])
  except (json.JSONDecodeError, KeyError, TypeError, ValueError) as err:
    raise FingerprintError(f"could not parse fpcalc output for {path}: "
                           f"{err}") from err
  if not isinstance(raw_values, list) or not raw_values:
    raise FingerprintError(f"fpcalc found no audio in {path}")
  return Fingerprint(values=tuple(int(value) for value in raw_values),
                     duration=duration,
                     algorithm=algorithm)


def similarity(first: Fingerprint, second: Fingerprint) -> float:
  """Scores how much of two fingerprints line up, from 0.0 to 1.0.

  Slides one fingerprint against the other and, for every alignment,
  counts the positions whose sub-fingerprints differ by at most
  `MAX_BIT_ERROR` bits. The best alignment's count, over the length of
  the shorter fingerprint, is the score. This is the comparison
  chromaprint itself defines; a re-encode of the same audio scores near
  1.0 while unrelated tracks score near 0.

  Args:
    first: One fingerprint.
    second: The other.

  Returns:
    The score, 0.0 if either fingerprint is empty.

  Raises:
    FingerprintError: If the two were produced by different chromaprint
      algorithms, which makes them incomparable.
  """
  if first.algorithm != second.algorithm:
    raise FingerprintError(f"cannot compare algorithm {first.algorithm} with "
                           f"{second.algorithm} fingerprints")
  if first.is_empty() or second.is_empty():
    return 0.0

  left = first.values
  right = second.values
  counts: dict[int, int] = {}
  best = 0
  for index, value in enumerate(left):
    begin = max(0, index - MAX_ALIGN_OFFSET)
    end = min(len(right), index + MAX_ALIGN_OFFSET)
    for other in range(begin, end):
      if (value ^ right[other]).bit_count() <= MAX_BIT_ERROR:
        offset = index - other
        count = counts.get(offset, 0) + 1
        counts[offset] = count
        best = max(best, count)
  return best / min(len(left), len(right))


def shared_values(fingerprints: Iterable[Sequence[int]]) -> int:
  """Counts sub-fingerprints common to every input.

  Used as a cheap candidate filter before the full `similarity` scan,
  which is far too slow to run over every pair in a 2000-track library.

  Args:
    fingerprints: The raw sub-fingerprint sequences to intersect.

  Returns:
    How many distinct sub-fingerprints appear in all of them, 0 if none
    were given.
  """
  common: set[int] | None = None
  for values in fingerprints:
    common = set(values) if common is None else common & set(values)
    if not common:
      return 0
  return len(common) if common else 0
