"""Validate spec.md self-consistency.

Dangling section references have shipped twice and a mangled heading once;
this runs in CI to stop the third. See spec.md §20.
"""

import re
import sys
from pathlib import Path

STALE = [
  "cookies from file",
  "20 kHz lowpass",
  "transcode → FLAC",
  "probe/make_probe",
]


def main() -> int:
  """Check headings, cross-references and known-stale claims."""
  spec = Path(__file__).resolve().parent.parent / "spec.md"
  text = spec.read_text(encoding="utf-8")
  errors = []

  headings = [int(m.group(1)) for m in re.finditer(r"^## (\d+)\.", text, re.M)]
  if headings != list(range(1, len(headings) + 1)):
    errors.append(f"section numbers not contiguous from 1: {headings}")

  refs = {int(m.group(1)) for m in re.finditer(r"§(\d+)", text)}
  dangling = sorted(refs - set(headings))
  if dangling:
    errors.append(f"dangling references: {dangling}")

  for phrase in STALE:
    if phrase in text:
      errors.append(f"stale claim present: {phrase!r}")

  for line in text.splitlines():
    if re.match(r"^#{1,3} [a-z]*[A-Z]", line) and not re.search(
      r"§|ID3|FLAC|AIFF|BPM", line
    ):
      errors.append(f"heading may be mangled: {line!r}")

  for e in errors:
    print(f"check_spec: {e}", file=sys.stderr)
  print("check_spec: ok" if not errors else f"check_spec: {len(errors)} problem(s)")
  return 1 if errors else 0


if __name__ == "__main__":
  raise SystemExit(main())
