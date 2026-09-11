"""Measure field coverage: how complete are the tags we can actually produce?

Distinct from the accuracy measurement. Accuracy asks *do we know which track
this is*; coverage asks *how many of the fields can we fill*. SPEC.md §2 is a
coverage goal, so both matter.
"""

import argparse
import collections
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from music import config, db  # noqa: E402
from music.identify import should_auto_accept  # noqa: E402
from music.normalise import normalise  # noqa: E402
from music.publish.tag import FRAME_MAP  # noqa: E402
from music.resolve import Resolver  # noqa: E402
from music.sources.acoustid import AcoustId  # noqa: E402
from music.sources.base import Identity  # noqa: E402
from music.sources.discogs import Discogs  # noqa: E402
from music.sources.itunes import ITunes  # noqa: E402
from music.sources.musicbrainz import MusicBrainz  # noqa: E402
from music.sources.spotify import Spotify  # noqa: E402


def main() -> int:
  """Run the coverage measurement."""
  parser = argparse.ArgumentParser()
  parser.add_argument("library_jsonl")
  parser.add_argument("--n", type=int, default=30)
  parser.add_argument("--seed", type=int, default=99)
  args = parser.parse_args()

  rows = [json.loads(x) for x in Path(args.library_jsonl).read_text().splitlines()]
  random.seed(args.seed)
  sample = random.sample(rows, args.n)

  cfg = config.load()
  conn = db.connect(cfg.paths.database)
  db.migrate(conn)
  creds = cfg.credentials
  mb = MusicBrainz(conn)
  extra: list[object] = [ITunes(conn)]
  if creds.get("DISCOGS_TOKEN"):
    extra.append(Discogs(conn, creds["DISCOGS_TOKEN"]))
  if creds.get("SPOTIFY_CLIENT_ID"):
    extra.append(
      Spotify(conn, creds["SPOTIFY_CLIENT_ID"], creds["SPOTIFY_CLIENT_SECRET"])
    )
  resolver = Resolver(
    conn,
    mb,
    AcoustId(conn, creds["ACOUSTID_API_KEY"])
    if creds.get("ACOUSTID_API_KEY")
    else None,
    extra,
  )
  audio_dir = Path.home() / "Music" / "yt-dlp"

  filled: collections.Counter = collections.Counter()
  by_source: collections.Counter = collections.Counter()
  accepted = 0
  for row in sample:
    cleaned = normalise(row["tags"].get("artist", ""), row["tags"].get("title", ""))
    if cleaned.is_empty:
      continue
    identity = Identity(
      artist=cleaned.artist,
      title=cleaned.title,
      artist_full=cleaned.artist_full,
      duration_s=float(row["dur"]) if row.get("dur") else None,
    )
    audio = audio_dir / row["file"]
    result = resolver.resolve(identity, audio if audio.exists() else None)
    if should_auto_accept(result.match):
      accepted += 1
    seen = set()
    for candidate in result.candidates:
      if candidate.value and candidate.field not in seen:
        seen.add(candidate.field)
        filled[candidate.field] += 1
        by_source[candidate.source] += 1

  total = len(sample)
  print(f"\n=== field coverage over {total} tracks ===")
  print(
    f"  identity auto-accepted: {accepted}/{total} ({100 * accepted / total:.0f}%)\n"
  )
  for field_name in sorted(FRAME_MAP):
    count = filled.get(field_name, 0)
    bar = "#" * round(20 * count / total)
    print(f"  {field_name:<16} {count:>3}/{total}  {100 * count / total:>3.0f}%  {bar}")
  print("\n  candidates contributed by source:")
  for source, count in by_source.most_common():
    print(f"    {source:<14} {count:>4}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
