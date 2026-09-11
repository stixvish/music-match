"""cp5 — check genre-family routing on the library's weakest material.

SPEC.md §7 flags the ~105 Bollywood tracks as both the least reliable routing
and the thinnest source coverage. If families are wrong there, arbitration
silently applies the wrong precedence table to them.
"""

import argparse
import collections
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from music.classify import Classifier, family_for_identity  # noqa: E402

BOLLYWOOD_TAGS = ("bollywood", "indian pop", "indian", "desi")


def main() -> int:
  """Run the gate."""
  parser = argparse.ArgumentParser()
  parser.add_argument("library_jsonl")
  parser.add_argument("--n", type=int, default=30)
  parser.add_argument("--seed", type=int, default=5)
  args = parser.parse_args()

  rows = [json.loads(x) for x in Path(args.library_jsonl).read_text().splitlines()]
  bollywood = [
    r for r in rows if (r["tags"].get("genre", "") or "").casefold() in BOLLYWOOD_TAGS
  ]
  print(f"bollywood-tagged tracks in library: {len(bollywood)}")
  random.seed(args.seed)
  sample = random.sample(bollywood, min(args.n, len(bollywood)))

  audio_dir = Path.home() / "Music" / "yt-dlp"
  classifier = Classifier()
  families: collections.Counter = collections.Counter()
  genres: collections.Counter = collections.Counter()
  shown = []
  for row in sample:
    path = audio_dir / row["file"]
    if not path.exists():
      families["file missing"] += 1
      continue
    try:
      top = classifier.predict(path, top=1)[0]
    except Exception as exc:  # noqa: BLE001 - report, do not abort the gate
      families["error"] += 1
      print(f"  error on {row['file'][:40]}: {exc}", file=sys.stderr)
      continue
    # the isrc override applies once identity is known (SPEC.md §7)
    isrc = row["tags"].get("isrc")
    family = family_for_identity(top.genre, isrc)
    families[family] += 1
    genres[top.genre] += 1
    if len(shown) < 12:
      shown.append((row["file"][:40], top.genre, isrc or "-", family))

  print("\n=== family routing ===")
  total = sum(families.values())
  for family, count in families.most_common():
    print(f"  {family:<14} {count:>3}  ({100 * count / total:.0f}%)")
  print("\n=== top-level genres predicted ===")
  for genre, count in genres.most_common(8):
    print(f"  {genre:<28} {count:>3}")
  print("\n  examples:")
  for name, genre, style, family in shown:
    print(f"    {name:<40} {genre:<22} {style[:12]:<12} -> {family}")

  # the gate: bollywood must land consistently, not scatter across families.
  dominant = families.most_common(1)[0][1] if families else 0
  share = 100 * dominant / total if total else 0
  print(f"\n  dominant family share: {share:.0f}%")
  print("  gate:", "PASS" if share >= 60 else "FAIL — routing is scattered")
  return 0 if share >= 60 else 1


if __name__ == "__main__":
  raise SystemExit(main())
