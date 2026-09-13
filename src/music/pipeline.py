"""The acquisition pipeline: url in, tagged files and review items out.

Lives apart from `cli` because it has two drivers. The command line runs it in
the foreground; the web ui runs it on a worker thread and watches `Progress`
(SPEC.md §15). Extracting it is what stopped "add a track" from being a
terminal-only capability.
"""

import logging
import re
import sqlite3
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from music import config, db
from music.acquire import (
  Download,
  QualityError,
  VideoRef,
  already_have,
  classify_download,
  download,
  enumerate_playlist,
  flag_video_rip,
  refresh_cookies,
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


class LogBuffer:
  """A bounded, append-only log the ui polls incrementally.

  Written by the worker thread and read by request handlers, so every access
  is locked. Lines carry a monotonic sequence number: the client asks for
  everything after the last one it saw rather than re-fetching the whole
  buffer on a 1-second poll.

  The live download percentage is deliberately *not* a line. It changes several
  times a second and would bury every real event within seconds; it lives in
  `Progress.progress_line` and is overwritten in place, the way a terminal
  rewrites a line with a carriage return.
  """

  def __init__(self, capacity: int = 4000) -> None:
    """Create a buffer.

    Args:
      capacity: Maximum lines retained; the oldest are dropped.
    """
    self._lines: deque[tuple[int, str, str]] = deque(maxlen=capacity)
    self._next = 0
    self._lock = threading.Lock()

  def add(self, text: str, level: str = "info") -> None:
    """Append a line.

    Args:
      text: The line. Trailing whitespace is stripped; blanks are ignored.
      level: One of `info`, `warn`, `error`, `step`.
    """
    line = (text or "").rstrip()
    if not line:
      return
    with self._lock:
      self._lines.append((self._next, level, line[:500]))
      self._next += 1

  def since(self, seq: int) -> tuple[list[dict[str, object]], int]:
    """Read lines newer than `seq`.

    Args:
      seq: The last sequence number the caller has seen; -1 for everything.

    Returns:
      The new lines and the sequence number to pass next time.
    """
    with self._lock:
      new = [
        {"seq": n, "level": level, "text": text}
        for n, level, text in self._lines
        if n > seq
      ]
      return new, self._next - 1


class YtdlSink:
  """Adapts `LogBuffer` to the logger interface yt-dlp expects."""

  def __init__(self, buffer: LogBuffer, progress: Progress) -> None:
    """Bind the sink to a buffer and the run it is reporting on.

    Args:
      buffer: Where discrete lines go.
      progress: Where the live percentage goes.
    """
    self._buffer = buffer
    self._progress = progress

  def debug(self, msg: str) -> None:
    """yt-dlp routes both debug and info here; only `[debug]` is real debug."""
    if not msg.startswith("[debug] "):
      self._buffer.add(msg)

  def info(self, msg: str) -> None:
    """Record an informational line."""
    self._buffer.add(msg)

  def warning(self, msg: str) -> None:
    """Record a warning, escalating the one that stops a run dead."""
    if "cookies are no longer valid" in msg:
      # yt-dlp checks after every request whether LOGIN_INFO is still in the
      # jar. YouTube rotates account cookies on open tabs, so ordinary browsing
      # invalidates whatever was read from that profile — and re-reading it
      # more often does not help, because the browsing is the cause.
      self._buffer.add(msg, "error")
      self._buffer.add(
        "  YouTube rotated the cookies. Re-reading the browser will not fix"
        " this: export once from a private window instead and set"
        " youtube.cookie_file (SPEC.md §6).",
        "error",
      )
      self._progress.cookies_invalid = True
      return
    self._buffer.add(msg, "warn")

  def error(self, msg: str) -> None:
    """Record an error."""
    self._buffer.add(msg, "error")

  def hook(self, event: dict[str, object]) -> None:
    """Progress callback: update the live line, and log the finish.

    Args:
      event: yt-dlp's progress dictionary.
    """
    status = event.get("status")
    if status == "downloading":
      percent = str(event.get("_percent_str") or "").strip()
      speed = str(event.get("_speed_str") or "").strip()
      total = str(event.get("_total_bytes_str") or "").strip()
      self._progress.progress_line = (
        f"[download] {percent} of {total} at {speed}".replace("  ", " ")
      )
    elif status == "finished":
      self._progress.progress_line = ""
      self._buffer.add("[download] complete")


@dataclass
class Progress:
  """Live state of one ingest run, safe to serialise straight to the ui."""

  url: str = ""
  total: int = 0
  done: int = 0
  # per-stage counters. `done` alone cannot distinguish "downloading track 40"
  # from "waiting on musicbrainz for track 40", and those feel very different
  # when you are watching a hundred tracks go by.
  downloaded: int = 0
  analysed: int = 0
  resolved: int = 0
  published: int = 0
  queued: int = 0
  skipped: int = 0
  failed: int = 0
  current: str = ""
  stage: str = ""
  progress_line: str = ""
  finished: bool = False
  cookies_refreshed: bool = False
  cookies_invalid: bool = False
  error: str = ""
  failures: list[str] = field(default_factory=list)

  def as_dict(self) -> dict[str, object]:
    """Serialise for the ui, without the fields it has no use for."""
    return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


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
  # the catalogue's own genre and the label both name regional traditions the
  # classifier has no class for; the classifier still decides everything else
  catalogue_genre = next(
    (c.value for c in candidates if c.field == "genre" and c.source == "itunes"),
    next((c.value for c in candidates if c.field == "genre"), ""),
  )
  family = family_for_identity(
    prediction.genre if prediction else "",
    by_field.get("isrc"),
    catalogue_genre=catalogue_genre,
    label=by_field.get("label", ""),
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
  if prediction and prediction.genre:
    # The classifier's *top-level* genre is what routes. Only the style was
    # stored, so the routing input was unrecoverable and a family could never
    # be recomputed offline the way arbitration can (SPEC.md §11). It is not an
    # ID3 frame; `fields.NON_TAG_FIELDS` keeps it out of the file.
    candidates.append(
      FieldCandidate(
        field="classifier_genre",
        value=prediction.genre,
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
  log_buffer: LogBuffer | None = None,
  on_change: Callable[[Progress], None] | None = None,
) -> Progress:
  """Download everything at `url` and run it through the pipeline.

  One bad track never stops a run; failures are counted and reported at the
  end. A track the resolver is unsure about is queued for review rather than
  published (SPEC.md §12).

  Args:
    url: One or more YouTube / YouTube Music video or playlist links,
      separated by whitespace or commas. Duplicates across them are removed.
    cfg: Runtime configuration.
    limit: Stop after this many entries; 0 means all of them.
    conn: Open connection. One is opened if omitted — pass your own from a
      worker thread, since SQLite connections are not shared across threads.
    resolver: Configured resolver, built if omitted.
    progress: State object to update in place, so a caller on another thread
      can watch it.
    log_buffer: Where yt-dlp's own output and the stage transitions are
      mirrored, for the ui's console.
    on_change: Called after every track, for callers that want to push.

  Returns:
    The final Progress.
  """
  state = progress or Progress()
  state.url = url
  conn = conn or open_db(cfg)
  resolver = resolver or build_resolver(conn, cfg)
  buffer = log_buffer or LogBuffer()
  sink = YtdlSink(buffer, state)

  def changed() -> None:
    if on_change:
      on_change(state)

  def step(stage: str, text: str) -> None:
    state.stage = stage
    buffer.add(text, "step")
    changed()

  # several links at once: whitespace- or comma-separated. Enumerating them
  # together, and de-duplicating across them, means a track appearing on two
  # playlists is downloaded once rather than downloaded and then skipped.
  urls = [u for u in re.split(r"[\s,]+", url) if u]
  # Fresh cookies for every run. YouTube rotates session cookies, so a jar
  # kept for the life of the process goes stale — and the web ui's server is a
  # process that stays up for days.
  refresh_cookies()
  step("enumerate", f"$ music ingest {' '.join(urls)}")
  try:
    refs = []
    seen: set[str] = set()
    for one in urls:
      for ref in enumerate_playlist(one, cfg.youtube, sink):
        if ref.video_id not in seen:
          seen.add(ref.video_id)
          refs.append(ref)
      if len(urls) > 1:
        buffer.add(f"  {one}: {len(refs)} unique so far")
  except Exception as exc:  # noqa: BLE001 - a bad url is a message, not a crash
    state.error = str(exc)[:300]
    state.finished = True
    buffer.add(str(exc)[:300], "error")
    changed()
    return state
  if not refs:
    state.error = f"no entries found at {url}"
    state.finished = True
    buffer.add(state.error, "error")
    changed()
    return state

  if limit:
    refs = refs[:limit]
  state.total = len(refs)
  buffer.add(f"found {len(refs)} track(s)", "step")
  changed()

  for index, ref in enumerate(refs, 1):
    state.current = ref.title[:80]
    changed()
    try:
      if already_have(conn, ref.video_id):
        log.info("skip (already have): %s", ref.title[:60])
        buffer.add(f"[{index}/{len(refs)}] skip, already have: {ref.title[:60]}")
        state.skipped += 1
        continue
      step("download", f"[{index}/{len(refs)}] {ref.title[:60]}")
      item = _download_with_fresh_cookies(ref, cfg, sink, buffer, state)
      track_id = register(conn, item)
      state.downloaded += 1
      changed()

      verdict = classify_download(item)
      flag_video_rip(conn, track_id, verdict)
      if verdict.is_video_rip:
        log.warning("  video rip (%s): %s", ",".join(verdict.reasons), item.title[:44])
        buffer.add(f"  video rip ({','.join(verdict.reasons)})", "warn")

      step("resolve", f"  identifying — {item.abr:.0f} kbps, itag {item.itag}")
      accepted = resolve_and_arbitrate(conn, track_id, item, resolver)
      # analysis and resolution finish together; counting them apart would
      # imply a queue between them that does not exist.
      state.analysed += 1
      state.resolved += 1
      _set_stage(conn, track_id, "resolved")
      if not accepted:
        log.info("  review needed: %s", item.title[:52])
        buffer.add("  queued for review (low confidence)", "warn")
        state.queued += 1
        continue

      step("publish", "  tagging and filing")
      dest = publish_track(conn, track_id, cfg.paths.library, cfg.paths.staging)
      _set_stage(conn, track_id, "published")
      log.info("published: %s", dest.name)
      buffer.add(f"  published: {dest.name}")
      state.published += 1
    except Exception as exc:  # noqa: BLE001 - one bad track must not stop a run
      log.error("failed %s: %s", ref.title[:50], exc)
      buffer.add(f"  failed: {exc}"[:200], "error")
      state.failed += 1
      state.failures.append(f"{ref.title[:60]}: {exc}"[:200])
    finally:
      state.done += 1
      state.progress_line = ""
      changed()

  state.current = ""
  state.stage = ""
  state.finished = True
  buffer.add(
    f"done: {state.published} published, {state.queued} queued,"
    f" {state.skipped} skipped, {state.failed} failed",
    "step",
  )
  changed()
  log.info(
    "done: %d published, %d queued for review, %d skipped, %d failed",
    state.published,
    state.queued,
    state.skipped,
    state.failed,
  )
  return state


def _set_stage(conn: sqlite3.Connection, track_id: int, stage: str) -> None:
  """Record how far a track got, so an interrupted run can be resumed.

  Args:
    conn: Open connection.
    track_id: Track to mark.
    stage: The stage just completed.
  """
  conn.execute(
    "UPDATE track SET stage = ?, updated_at = datetime('now') WHERE id = ?",
    (stage, track_id),
  )


def _download_with_fresh_cookies(
  ref: VideoRef,
  cfg: config.Config,
  sink: object,
  buffer: LogBuffer,
  state: Progress,
) -> Download:
  """Download one track, re-reading cookies once if the stream is downgraded.

  A run over the full library takes hours, so cookies can rotate part way
  through. The symptom is specific and already named in `download`: the best
  available stream drops below the bitrate floor, because YouTube is serving
  the unauthenticated formats. Retrying *that* signal is better than guessing
  a refresh interval — it responds to the thing that actually went wrong.

  One retry only. If fresh cookies do not restore the premium formats, the
  problem is the account or the browser session, and quietly re-reading the
  keychain on a loop would hide it.

  Args:
    ref: The video to fetch.
    cfg: Runtime configuration.
    sink: yt-dlp logger, so the retry is visible in the console.
    buffer: Run log.
    state: Run state, which remembers whether the refresh already happened.

  Returns:
    The download.

  Raises:
    QualityError: If the floor is still not met after a refresh.
  """
  try:
    return download(ref.video_id, cfg.paths.staging, cfg.youtube, sink)
  except QualityError as exc:
    if state.cookies_refreshed:
      # Already tried. If every track is being downgraded the cause is the
      # account or the session, not the jar, and re-reading the keychain 2,300
      # times would turn one problem into a second one.
      raise
    state.cookies_refreshed = True
    buffer.add(f"  {exc}", "warn")
    buffer.add("  re-reading cookies and retrying — they may have rotated", "step")
    log.warning("quality floor missed; refreshing cookies and retrying once")
    refresh_cookies()
    return download(ref.video_id, cfg.paths.staging, cfg.youtube, sink)
