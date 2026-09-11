"""The acquisition pipeline: url in, tagged files and review items out.

Lives apart from `cli` because it has two drivers. The command line runs it in
the foreground; the web ui runs it on a worker thread and watches `Progress`
(SPEC.md §15). Extracting it is what stopped "add a track" from being a
terminal-only capability.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from music import config, db
from music.acquire import (
  Download,
  already_have,
  classify_download,
  download,
  enumerate_playlist,
  flag_video_rip,
  register,
)
from music.arbitrate import arbitrate, persist
from music.classify import family_for_identity
from music.identify import confidence, review_reason, should_auto_accept
from music.normalise import normalise
from music.publish import publish_track
from music.publish.naming import extract_version
from music.resolve import Resolver
from music.sources import build_enrichment_sources
from music.sources.acoustid import AcoustId
from music.sources.base import FieldCandidate, Identity
from music.sources.musicbrainz import MusicBrainz

if TYPE_CHECKING:  # the classify group is optional and heavy
  from music.classify import Classifier, Prediction

log = logging.getLogger("music")

# loaded once; pulls in tensorflow and ~20 MB of graphs
_CLASSIFIER: Classifier | None = None
_CLASSIFY_WARNED = False


@dataclass
class Progress:
  """Live state of one ingest run, safe to serialise straight to the ui."""

  url: str = ""
  total: int = 0
  done: int = 0
  published: int = 0
  queued: int = 0
  skipped: int = 0
  failed: int = 0
  current: str = ""
  finished: bool = False
  error: str = ""
  failures: list[str] = field(default_factory=list)


def _classified_genre(path: Path) -> Prediction | None:
  """Top-level genre from the local classifier, for precedence routing.

  Returns an empty string when Essentia is not installed — the optional
  `classify` dependency group is heavy, and a run without it should degrade to
  the ISRC override plus the `other` family rather than fail.

  Args:
    path: Audio file.

  Returns:
    The top prediction, or None when Essentia is unavailable.
  """
  global _CLASSIFIER
  try:
    if _CLASSIFIER is None:
      from music.classify import Classifier

      _CLASSIFIER = Classifier()
    predictions = _CLASSIFIER.predict(path, top=1)
  except Exception as exc:  # noqa: BLE001 - routing degrades, never fails a run
    global _CLASSIFY_WARNED
    if not _CLASSIFY_WARNED:
      # warn once: silent degradation looks identical to "everything is
      # genuinely `other`", which is how this went unnoticed the first time.
      log.warning(
        "genre classification unavailable (%s); all tracks will route to the "
        "`other` precedence table. install with: uv sync --group classify",
        exc,
      )
      _CLASSIFY_WARNED = True
    return None
  return predictions[0] if predictions else None


def resolve_and_arbitrate(
  conn: sqlite3.Connection,
  track_id: int,
  item: Download,
  resolver: Resolver,
) -> bool:
  """Run the full resolution path for one track and store the result.

  Args:
    conn: Open connection.
    track_id: Track being processed.
    item: The download, for its title and channel.
    resolver: Configured resolver.

  Returns:
    True if the identity was confident enough to publish (SPEC.md §12).
  """
  # yt-dlp gives channel and title; the channel is the better artist signal,
  # and normalise strips the VEVO/Topic noise and any redundant "Artist - "
  cleaned = normalise(item.channel, item.title)
  identity = Identity(
    artist=cleaned.artist,
    title=cleaned.title,
    artist_full=cleaned.artist_full,
    duration_s=item.duration_s or None,
  )
  result = resolver.resolve(identity, item.path)

  candidates = list(result.candidates)
  # remixer and mix name come from the title we already have (SPEC.md §14)
  version = extract_version(item.title)
  if version.mix_name:
    candidates.append(
      FieldCandidate(field="mix_name", value=version.mix_name, source="derived")
    )
  if version.remixer:
    candidates.append(
      FieldCandidate(field="remixer", value=version.remixer, source="derived")
    )

  # classification runs BEFORE candidates are stored, so essentia's genre is
  # recorded for provenance like any other source. appending it afterwards let
  # it win arbitration while never appearing in the review ui.
  #
  # the family is finalised here, not at classify time: the ISRC override
  # needs an ISRC, which only exists once identity is resolved (SPEC.md §6).
  #
  # the classifier's TOP-LEVEL genre is what routes, not the resolved genre
  # field — that holds a Discogs *style* ("Dance-pop", "Hip-House") which is
  # not in the family map and silently routed everything to `other`.
  by_field = {c.field: c.value for c in candidates}
  prediction = _classified_genre(item.path)
  family = family_for_identity(
    prediction.genre if prediction else "", by_field.get("isrc")
  )
  # essentia is the last-resort genre source (SPEC.md §7): it ranks below
  # every real source, but it describes *this audio* rather than whichever
  # release a catalogue happened to match.
  if prediction and prediction.style:
    candidates.append(
      FieldCandidate(
        field="genre",
        value=prediction.style,
        source="essentia",
        confidence=float(prediction.activation),
      )
    )
  conn.execute("UPDATE track SET genre_family=? WHERE id=?", (family, track_id))
  conn.execute(
    "UPDATE track SET identity_confidence=?, norm_artist=?, norm_title=?,"
    " acoustid=?, mb_recording_id=?, updated_at=datetime('now') WHERE id=?",
    (
      confidence(result.match),
      cleaned.artist,
      cleaned.title,
      result.acoustid or None,
      result.mb_recording_id or None,
      track_id,
    ),
  )
  for candidate in candidates:
    conn.execute(
      "INSERT INTO field_candidate (track_id, field, value, source, confidence)"
      " VALUES (?,?,?,?,?)",
      (
        track_id,
        candidate.field,
        candidate.value,
        candidate.source,
        candidate.confidence,
      ),
    )

  if not should_auto_accept(result.match):
    reason = review_reason(result.match) or "low_confidence"
    conn.execute(
      "INSERT OR IGNORE INTO review_queue (track_id, reason) VALUES (?,?)",
      (track_id, reason),
    )
    conn.execute("UPDATE track SET status='review' WHERE id=?", (track_id,))
    return False

  persist(conn, track_id, arbitrate(candidates, family=family))
  return True


def open_db(cfg: config.Config) -> sqlite3.Connection:
  """Open and migrate the pipeline database.

  Args:
    cfg: Runtime configuration.

  Returns:
    An open, migrated connection.
  """
  conn = db.connect(cfg.paths.database)
  db.migrate(conn)
  return conn


def build_resolver(conn: sqlite3.Connection, cfg: config.Config) -> Resolver:
  """Assemble the resolver with every source the credentials allow.

  Args:
    conn: Open connection, used for the response cache.
    cfg: Runtime configuration.

  Returns:
    A configured Resolver.
  """
  key = cfg.credentials.get("ACOUSTID_API_KEY", "")
  return Resolver(
    conn,
    MusicBrainz(conn),
    AcoustId(conn, key) if key else None,
    build_enrichment_sources(conn, cfg),
  )


def ingest(
  url: str,
  cfg: config.Config,
  *,
  limit: int = 0,
  conn: sqlite3.Connection | None = None,
  resolver: Resolver | None = None,
  progress: Progress | None = None,
  on_change: Callable[[Progress], None] | None = None,
) -> Progress:
  """Download everything at `url` and run it through the pipeline.

  One bad track never stops a run; failures are counted and reported at the
  end. A track the resolver is unsure about is queued for review rather than
  published (SPEC.md §12).

  Args:
    url: A YouTube or YouTube Music video or playlist link.
    cfg: Runtime configuration.
    limit: Stop after this many entries; 0 means all of them.
    conn: Open connection. One is opened if omitted — pass your own from a
      worker thread, since SQLite connections are not shared across threads.
    resolver: Configured resolver, built if omitted.
    progress: State object to update in place, so a caller on another thread
      can watch it.
    on_change: Called after every track, for callers that want to push.

  Returns:
    The final Progress.
  """
  state = progress or Progress()
  state.url = url
  conn = conn or open_db(cfg)
  resolver = resolver or build_resolver(conn, cfg)

  def changed() -> None:
    if on_change:
      on_change(state)

  try:
    refs = enumerate_playlist(url, cfg.youtube)
  except Exception as exc:  # noqa: BLE001 - a bad url is a message, not a crash
    state.error = str(exc)[:300]
    state.finished = True
    changed()
    return state
  if not refs:
    state.error = f"no entries found at {url}"
    state.finished = True
    changed()
    return state

  if limit:
    refs = refs[:limit]
  state.total = len(refs)
  changed()

  for ref in refs:
    state.current = ref.title[:80]
    changed()
    try:
      if already_have(conn, ref.video_id):
        log.info("skip (already have): %s", ref.title[:60])
        state.skipped += 1
        continue
      item = download(ref.video_id, cfg.paths.staging, cfg.youtube)
      track_id = register(conn, item)
      verdict = classify_download(item)
      flag_video_rip(conn, track_id, verdict)
      if verdict.is_video_rip:
        log.warning("  video rip (%s): %s", ",".join(verdict.reasons), item.title[:44])
      if not resolve_and_arbitrate(conn, track_id, item, resolver):
        log.info("  review needed: %s", item.title[:52])
        state.queued += 1
        continue
      dest = publish_track(conn, track_id, cfg.paths.library, cfg.paths.staging)
      log.info("published: %s", dest.name)
      state.published += 1
    except Exception as exc:  # noqa: BLE001 - one bad track must not stop a run
      log.error("failed %s: %s", ref.title[:50], exc)
      state.failed += 1
      state.failures.append(f"{ref.title[:60]}: {exc}"[:200])
    finally:
      state.done += 1
      changed()

  state.current = ""
  state.finished = True
  changed()
  log.info(
    "done: %d published, %d queued for review, %d skipped, %d failed",
    state.published,
    state.queued,
    state.skipped,
    state.failed,
  )
  return state
