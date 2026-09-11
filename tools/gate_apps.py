"""cp6 — prepare published tracks for verification in Rekordbox and Serato.

This gate cannot be automated: it asks whether two GUI applications display
what we wrote. The script assembles the evidence and prints exactly what to
check, so the manual step is short and unambiguous.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from music import config, db  # noqa: E402
from music.publish import tag  # noqa: E402

# every frame rekordbox was measured to read from AIFF (SPEC.md §10)
EXPECTED = (
  "title",
  "artist",
  "album",
  "album_artist",
  "genre",
  "track_number",
  "disc_number",
  "composer",
  "lyricist",
  "remixer",
  "mix_name",
  "label",
  "original_artist",
  "key",
  "isrc",
  "year",
  "release_date",
)


def main() -> int:
  """Report what was written, and what to verify by hand."""
  parser = argparse.ArgumentParser()
  parser.add_argument("--limit", type=int, default=20)
  args = parser.parse_args()

  cfg = config.load()
  conn = db.connect(cfg.paths.database)
  db.migrate(conn)
  rows = conn.execute(
    "SELECT id, published_path, genre_family FROM track"
    " WHERE published_path IS NOT NULL ORDER BY id LIMIT ?",
    (args.limit,),
  ).fetchall()
  if not rows:
    print("nothing published yet — run `music ingest` first")
    return 1

  print(f"=== {len(rows)} published tracks in {cfg.paths.library} ===\n")
  coverage: dict[str, int] = dict.fromkeys(EXPECTED, 0)
  for row in rows:
    path = Path(row["published_path"])
    if not path.exists():
      print(f"  MISSING: {path}")
      continue
    tags = tag.read(path)
    present = [f for f in EXPECTED if tags.values.get(f)]
    for field in present:
      coverage[field] += 1
    print(f"  {path.name[:60]:<60} {len(present)}/{len(EXPECTED)} fields")

  print("\n=== field coverage across those tracks ===")
  for field, count in sorted(coverage.items(), key=lambda kv: -kv[1]):
    bar = "#" * round(18 * count / len(rows))
    print(f"  {field:<16} {count:>3}/{len(rows)}  {bar}")

  print(f"""
=== manual verification (cp6) ===

  1. import {cfg.paths.library} into rekordbox 7
  2. enable every metadata column
  3. confirm the fields above appear, and that non-zero ones are not blank
  4. import the same folder into serato dj lite
     serato has no columns for album artist, mix name, original artist,
     lyricist, disc or isrc — their absence there is expected (SPEC.md §10)
  5. edit one track's genre in the web ui, run `music retag`, then
     right-click -> Reload Tag in rekordbox and confirm the edit survived

  rekordbox caches tags per path: without Reload Tag it shows stale values.
""")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
