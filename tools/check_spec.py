"""Validate SPEC.md self-consistency.

Dangling section references have shipped twice and a mangled heading once;
this runs in CI to stop the third. See SPEC.md §20.
"""

import re
import subprocess
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
  spec = Path(__file__).resolve().parent.parent / "SPEC.md"
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

  errors.extend(_case_mismatches())

  for e in errors:
    print(f"check_spec: {e}", file=sys.stderr)
  print("check_spec: ok" if not errors else f"check_spec: {len(errors)} problem(s)")
  return 1 if errors else 0


def _case_mismatches() -> list[str]:
  """Find references to tracked files that use the wrong case.

  macOS is case-insensitive, so a mis-cased path resolves happily on a developer
  machine and then fails on a Linux CI runner. This has already happened once.
  Returns a list of human-readable problems.
  """
  out = subprocess.run(
    ["git", "ls-files"], capture_output=True, text=True, check=False
  ).stdout.split()
  tracked = {f.lower(): f for f in out}
  pattern = re.compile(r"[A-Za-z0-9_./-]+\.(?:md|py|toml|yml|yaml|sql)")
  problems = []
  for path in out:
    if path.startswith(".claude"):
      continue
    try:
      text = Path(path).read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
      continue
    for ref in sorted(set(pattern.findall(text))):
      actual = tracked.get(ref.lower())
      if actual and actual != ref:
        problems.append(f"{path}: refers to {ref!r}, tracked as {actual!r}")
  return problems


if __name__ == "__main__":
  raise SystemExit(main())
