"""Metadata sources, all behind one interface (SPEC.md §7).

`build_sources` is the only place that knows which adapters exist and what
credentials each needs. A source without credentials is simply absent — never a
crash, and never a silent half-configured state.
"""

import logging
import sqlite3

from music.config import Config
from music.sources.base import FieldCandidate, Identity, Source

__all__ = ["FieldCandidate", "Identity", "Source", "build_enrichment_sources"]

log = logging.getLogger(__name__)


def build_enrichment_sources(conn: sqlite3.Connection, cfg: Config) -> list[Source]:
  """Construct every enrichment source the configuration supports.

  Identity sources (MusicBrainz, AcoustID) are built separately by the
  resolver; these only fill fields.

  Args:
    conn: Open connection, shared for the response cache.
    cfg: Runtime configuration.

  Returns:
    Usable sources, in no particular order. Precedence is applied later.
  """
  from music.sources.beatport import Beatport, BeatportCredentialsError
  from music.sources.discogs import Discogs
  from music.sources.itunes import ITunes
  from music.sources.spotify import Spotify

  sources: list[Source] = [ITunes(conn)]
  creds = cfg.credentials

  if creds.get("DISCOGS_TOKEN"):
    sources.append(Discogs(conn, creds["DISCOGS_TOKEN"]))
  else:
    log.info("discogs disabled: no DISCOGS_TOKEN")

  if creds.get("SPOTIFY_CLIENT_ID") and creds.get("SPOTIFY_CLIENT_SECRET"):
    sources.append(
      Spotify(conn, creds["SPOTIFY_CLIENT_ID"], creds["SPOTIFY_CLIENT_SECRET"])
    )
  else:
    log.info("spotify disabled: no SPOTIFY_CLIENT_ID/SECRET")

  try:
    sources.append(
      Beatport(
        conn,
        creds.get("BEATPORT_CLIENT_ID", ""),
        creds.get("BEATPORT_CLIENT_SECRET", ""),
      )
    )
  except BeatportCredentialsError as exc:
    log.info("beatport disabled: %s", exc)

  return sources
