"""Measure high-confidence resolution rate (tasks/plan.md).

The 5-hour review budget in SPEC.md §9 rests on ~85% auto-accept, projected
from a measured 55% baseline. This measures the real number.

Run it at every checkpoint so the trend is visible. The thresholds below are
**binding only at cp4**, once every source exists and the audio has been
re-downloaded as art tracks; earlier readings are informational.

  >=75%   proceed
  65-75%  proceed, revise the budget
  <65%    stop and rethink
"""

import argparse
import collections
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from music import config, db  # noqa: E402
from music.identify import confidence, should_auto_accept  # noqa: E402
from music.normalise import normalise  # noqa: E402
from music.sources.base import Identity  # noqa: E402
from music.sources.musicbrainz import MusicBrainz  # noqa: E402

# coarse family buckets from the existing genre tags, for stratification only.
FAMILY = {
  "hip-hop": ("hip-hop/rap", "hip hop", "hip-hop", "rap"),
  "pop": ("pop", "dance / pop", "indie pop"),
  "r&b-soul": ("r&b/soul", "r&b"),
  "electronic": ("dance", "house", "electronic", "electronica", "edm", "mainstage"),
  "world": ("bollywood", "indian pop", "worldwide", "urbano latino"),
}


def family_of(genre: str) -> str:
  """Bucket a raw genre tag into a family."""
  low = (genre or "").lower()
  for name, values in FAMILY.items():
    if low in values:
      return name
  return "other"


def main() -> int:
  """Run the measurement."""
  parser = argparse.ArgumentParser()
  parser.add_argument("library_jsonl")
  parser.add_argument("--n", type=int, default=100)
  parser.add_argument("--seed", type=int, default=42)
  parser.add_argument(
    "--binding",
    action="store_true",
    help="apply the cp4 gate; without it the verdict is informational",
  )
  args = parser.parse_args()

  rows = [
    json.loads(line) for line in Path(args.library_jsonl).read_text().splitlines()
  ]
  buckets: dict[str, list] = collections.defaultdict(list)
  for row in rows:
    buckets[family_of(row["tags"].get("genre", ""))].append(row)

  random.seed(args.seed)
  per = max(1, args.n // len(buckets))
  sample = []
  for name, items in buckets.items():
    take = min(per, len(items))
    sample.extend((name, r) for r in random.sample(items, take))
  random.shuffle(sample)
  sample = sample[: args.n]

  cfg = config.load()
  conn = db.connect(cfg.paths.database)
  db.migrate(conn)
  source = MusicBrainz(conn)

  stats: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
  total = collections.Counter()
  for index, (family, row) in enumerate(sample, 1):
    cleaned = normalise(row["tags"].get("artist", ""), row["tags"].get("title", ""))
    if cleaned.is_empty:
      stats[family]["unusable"] += 1
      total["unusable"] += 1
      continue
    identity = Identity(
      artist=cleaned.artist,
      title=cleaned.title,
      duration_s=float(row["dur"]) if row.get("dur") else None,
    )
    match, _ = source.evaluate(identity)
    bucket = (
      "auto"
      if should_auto_accept(match)
      else ("no_match" if confidence(match) == 0 else "review")
    )
    stats[family][bucket] += 1
    total[bucket] += 1
    if index % 20 == 0:
      print(f"  ... {index}/{len(sample)}", file=sys.stderr)

  n = sum(total.values())
  auto = total["auto"]
  rate = 100 * auto / n if n else 0.0
  print(f"\n=== cp2: {n} tracks ===")
  for key in ("auto", "review", "no_match", "unusable"):
    print(f"  {key:<10} {total[key]:>4}  ({100 * total[key] / n:.0f}%)")
  print(f"\n  AUTO-ACCEPT RATE: {rate:.1f}%")
  print("\n  by family:")
  for family in sorted(stats):
    counts = stats[family]
    sub = sum(counts.values())
    print(
      f"    {family:<12} {counts['auto']:>3}/{sub:<3} "
      f"({100 * counts['auto'] / sub:.0f}%)"
    )
  verdict = (
    "PROCEED"
    if rate >= 75
    else "PROCEED, revise SPEC.md §9"
    if rate >= 65
    else "STOP AND RETHINK"
  )
  if args.binding:
    print(f"\n  gate (cp4, binding): {verdict}")
    return 0 if rate >= 65 else 1
  print(f"\n  reading: {rate:.1f}% — informational; cp4 is the binding gate")
  print(f"  would be: {verdict}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
