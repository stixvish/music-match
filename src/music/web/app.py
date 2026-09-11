"""HTTP surface for the review and editing UI (SPEC.md §15)."""

import sqlite3
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from music import config, db, elicit, pipeline
from music.arbitrate import Decision, arbitrate, persist
from music.publish import publish_track, retag, tag, transcode
from music.sources.base import FieldCandidate
from music.sources.url_override import parse as parse_url

STATIC = Path(__file__).parent / "static"

# Media types a browser will actually decode. `mimetypes` guesses
# `audio/mp4a-latm` for .m4a and `audio/x-aiff` for .aiff; Chrome reports an
# empty `canPlayType` for both, so the player silently sits at 0:00 instead of
# erroring. AIFF is absent deliberately: no browser plays it (SPEC.md §15).
PLAYABLE = {
  ".flac": "audio/flac",
  ".m4a": "audio/mp4",
  ".mp3": "audio/mpeg",
  ".mp4": "audio/mp4",
  ".ogg": "audio/ogg",
  ".opus": "audio/ogg",
  ".wav": "audio/wav",
  ".webm": "audio/webm",
}


class FieldEdit(BaseModel):
  """A manual override of one field."""

  field: str
  value: str


class UrlOverride(BaseModel):
  """An authoritative link pasted by the user."""

  url: str


class IngestRequest(BaseModel):
  """A YouTube or YouTube Music link to pull into the library."""

  url: str
  limit: int = 0


class Choice(BaseModel):
  """One elicitation answer: the index of the option picked, or -1."""

  option: int


def _rows(conn: sqlite3.Connection, sql: str, *args: Any) -> list[dict]:
  return [dict(r) for r in conn.execute(sql, args).fetchall()]


def create_app(database: Path | None = None) -> FastAPI:
  """Build the application.

  Args:
    database: Override the database path, for tests.

  Returns:
    A configured FastAPI app.
  """
  cfg = config.load()
  path = database or cfg.paths.database

  app = FastAPI(title="music", docs_url=None, redoc_url=None)

  def connect() -> sqlite3.Connection:
    conn = db.connect(path)
    db.migrate(conn)
    return conn

  @app.get("/", response_class=HTMLResponse)
  def index() -> HTMLResponse:
    return HTMLResponse(
      (STATIC / "index.html").read_text(encoding="utf-8"),
      headers={"Cache-Control": "no-store, max-age=0"},
    )

  # served with no-store: this is a local tool that is edited while running,
  # and a cached app.js silently shows stale behaviour that looks like a bug.
  no_cache = {"Cache-Control": "no-store, max-age=0"}

  @app.get("/app.css")
  def stylesheet() -> FileResponse:
    return FileResponse(STATIC / "app.css", media_type="text/css", headers=no_cache)

  @app.get("/app.js")
  def script() -> FileResponse:
    return FileResponse(
      STATIC / "app.js", media_type="text/javascript", headers=no_cache
    )

  @app.get("/api/tracks")
  def tracks(status: str | None = None, q: str | None = None) -> list[dict]:
    """List tracks, newest first, optionally filtered by status and a query.

    The search covers the *resolved* artist, title, album and label and also
    the normalised name the track was searched under. Both matter: finding a
    track by what it became is library browsing, and finding it by what YouTube
    called it is how a bad match gets chased down.

    Filtering happens outside the inner select because the resolved fields are
    correlated subqueries — SQLite cannot reference their aliases in the same
    WHERE clause.
    """
    conn = connect()
    inner = (
      "SELECT t.id, t.status, t.stage, t.genre_family, t.identity_confidence,"
      " t.is_video_rip, t.published_path, q.reason,"
      " (SELECT value FROM resolved_field"
      "  WHERE track_id=t.id AND field='artist') artist,"
      " (SELECT value FROM resolved_field"
      "  WHERE track_id=t.id AND field='title') title,"
      " (SELECT value FROM resolved_field"
      "  WHERE track_id=t.id AND field='album') album,"
      " (SELECT value FROM resolved_field"
      "  WHERE track_id=t.id AND field='label') label,"
      " t.norm_artist, t.norm_title, s.channel, s.duration_s"
      " FROM track t"
      " JOIN source_file s ON s.id = t.source_file_id"
      " LEFT JOIN review_queue q ON q.track_id = t.id"
    )
    clauses: list[str] = []
    args: list[object] = []
    if status:
      clauses.append("status = ?")
      args.append(status)
    term = (q or "").strip().casefold()
    if term:
      fields = (
        "artist",
        "title",
        "album",
        "label",
        "norm_artist",
        "norm_title",
        "channel",
      )
      clauses.append(
        "("
        + " OR ".join(f"lower(COALESCE({f}, '')) LIKE ? ESCAPE '\\'" for f in fields)
        + ")"
      )
      # `%` and `_` are LIKE metacharacters: a user typing `%` means a literal
      # percent sign, not "match everything".
      escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
      args.extend([f"%{escaped}%"] * len(fields))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return _rows(conn, f"SELECT * FROM ({inner}){where} ORDER BY id DESC", *args)

  @app.get("/api/track/{track_id}")
  def track(track_id: int) -> dict:
    """One track: resolved fields, every candidate, and provenance."""
    conn = connect()
    head = conn.execute(
      "SELECT t.*, s.staging_path, s.channel, s.duration_s, s.raw_tags"
      " FROM track t JOIN source_file s ON s.id = t.source_file_id"
      " WHERE t.id = ?",
      (track_id,),
    ).fetchone()
    if head is None:
      raise HTTPException(status_code=404, detail="no such track")
    candidates = _rows(
      conn,
      "SELECT field, value, source, confidence FROM field_candidate"
      " WHERE track_id=? ORDER BY field, source",
      track_id,
    )
    # what Accept would actually choose. showing a raw candidate instead makes
    # the button unpredictable: "Various Artists" reads as the album artist
    # even though arbitration rejects compilations outright (§7).
    preview = [
      {
        "field": d.field,
        "value": d.value,
        "source": d.source,
        "decided_by": d.decided_by,
      }
      for d in arbitrate(
        [
          FieldCandidate(
            field=c["field"],
            value=c["value"],
            source=c["source"],
            confidence=c["confidence"] or 1.0,
          )
          for c in candidates
          if c["value"]
        ],
        family=head["genre_family"] or "other",
      )
    ]
    return {
      "track": dict(head),
      "resolved": _rows(
        conn,
        "SELECT field, value, source, decided_by FROM resolved_field"
        " WHERE track_id=? ORDER BY field",
        track_id,
      ),
      "candidates": candidates,
      "preview": preview,
    }

  @app.post("/api/track/{track_id}/field")
  def edit_field(track_id: int, edit: FieldEdit) -> dict:
    """Set a field by hand. Marked `manual`, so no resolver run overwrites it."""
    conn = connect()
    conn.execute(
      "INSERT OR REPLACE INTO resolved_field"
      " (track_id, field, value, source, decided_by)"
      " VALUES (?,?,?,'manual','manual')",
      (track_id, edit.field, edit.value),
    )
    return {"ok": True, "field": edit.field, "value": edit.value}

  @app.post("/api/track/{track_id}/accept")
  def accept(track_id: int) -> dict:
    """Approve a reviewed track: arbitrate, publish, and clear the queue.

    A low-confidence track is never arbitrated by the pipeline (SPEC.md §12),
    so approving one has to do that work now — otherwise accepting would clear
    the queue and leave an untagged file that was never published.

    Manual edits survive: `persist` skips anything already decided by hand.
    """
    conn = connect()
    row = conn.execute(
      "SELECT genre_family, published_path FROM track WHERE id=?", (track_id,)
    ).fetchone()
    if row is None:
      raise HTTPException(status_code=404, detail="no such track")

    candidates = [
      FieldCandidate(
        field=r["field"],
        value=r["value"],
        source=r["source"],
        confidence=r["confidence"] or 1.0,
      )
      for r in conn.execute(
        "SELECT field, value, source, confidence FROM field_candidate WHERE track_id=?",
        (track_id,),
      )
      if r["value"]
    ]
    if candidates:
      persist(
        conn, track_id, arbitrate(candidates, family=row["genre_family"] or "other")
      )

    conn.execute("DELETE FROM review_queue WHERE track_id = ?", (track_id,))
    published = row["published_path"]
    if published:
      result = retag(conn, track_id)
      published = str(result) if result else published
    else:
      try:
        published = str(
          publish_track(conn, track_id, cfg.paths.library, cfg.paths.staging)
        )
      except Exception as exc:  # noqa: BLE001 - report, do not 500 the ui
        conn.execute(
          "UPDATE track SET status='failed', error=? WHERE id=?",
          (str(exc)[:300], track_id),
        )
        return {"ok": False, "error": str(exc)}
    conn.execute(
      "UPDATE track SET status='published', updated_at=datetime('now') WHERE id=?",
      (track_id,),
    )
    return {"ok": True, "published": published}

  @app.post("/api/track/{track_id}/url")
  def url_override(track_id: int, body: UrlOverride) -> dict:
    """Accept a pasted link as authoritative identity (SPEC.md §9)."""
    reference = parse_url(body.url)
    if reference is None:
      raise HTTPException(status_code=400, detail="unrecognised link")
    conn = connect()
    conn.execute(
      "INSERT OR REPLACE INTO resolved_field"
      " (track_id, field, value, source, decided_by)"
      " VALUES (?,'override_url',?,?, 'url_override')",
      (track_id, reference.identifier, reference.provider.value),
    )
    return {
      "ok": True,
      "provider": reference.provider.value,
      "kind": reference.kind,
      "id": reference.identifier,
    }

  @app.get("/api/audio/{track_id}")
  def audio(track_id: int) -> FileResponse:
    """Serve the audio so a reviewer can hear the track (SPEC.md §15).

    The download source is served in preference to the published file. It is
    what the browser can actually decode, and it is ~6x smaller than the AIFF,
    so the player is responsive. Only when staging has been cleared is a
    preview encoded from the published file.
    """
    conn = connect()
    row = conn.execute(
      "SELECT t.published_path, s.staging_path FROM track t"
      " JOIN source_file s ON s.id = t.source_file_id WHERE t.id = ?",
      (track_id,),
    ).fetchone()
    if row is None:
      raise HTTPException(status_code=404, detail="no such track")

    source = Path(row["staging_path"]) if row["staging_path"] else None
    if source and source.exists():
      media = PLAYABLE.get(source.suffix.lower())
      if media:
        return FileResponse(source, media_type=media, headers=no_cache)

    published = Path(row["published_path"]) if row["published_path"] else None
    if published and published.exists():
      preview = cfg.paths.staging / f"{track_id}.preview.m4a"
      # audio never changes once published — only tags do — so the preview is
      # cached indefinitely.
      if not preview.exists() and not transcode.to_preview(published, preview):
        raise HTTPException(status_code=503, detail="could not build a preview")
      return FileResponse(preview, media_type="audio/mp4", headers=no_cache)

    raise HTTPException(status_code=404, detail="audio file missing")

  @app.get("/api/artwork/{track_id}")
  def artwork(track_id: int) -> Response:
    """Serve the cover art embedded in a published file.

    A reviewer needs to see the art before approving it; a URL in a field is
    not a preview.
    """
    conn = connect()
    row = conn.execute(
      "SELECT published_path FROM track WHERE id = ?", (track_id,)
    ).fetchone()
    if row is None or not row["published_path"]:
      raise HTTPException(status_code=404, detail="not published")
    path = Path(row["published_path"])
    if not path.exists():
      raise HTTPException(status_code=404, detail="file missing")
    data = tag.read(path).artwork
    if not data:
      raise HTTPException(status_code=404, detail="no embedded artwork")
    return Response(content=data, media_type="image/jpeg", headers=no_cache)

  # One ingest at a time. Downloading is slow and rate-limited; two concurrent
  # runs would race on the same staging directory and double the request rate
  # against every source.
  job: dict[str, pipeline.Progress] = {}
  job_log = pipeline.LogBuffer()
  job_lock = threading.Lock()

  @app.post("/api/ingest")
  def start_ingest(request: IngestRequest) -> dict:
    """Download a video or playlist and run it through the pipeline.

    Returns immediately; the run happens on a worker thread and is watched
    through `/api/ingest/status`. A playlist takes minutes per track, so
    blocking the request would time out long before the work finished.
    """
    url = request.url.strip()
    if not url:
      raise HTTPException(status_code=400, detail="no url given")
    with job_lock:
      running = job.get("current")
      if running is not None and not running.finished:
        raise HTTPException(status_code=409, detail="an ingest is already running")
      state = pipeline.Progress(url=url)
      job["current"] = state

    def run() -> None:
      # its own connection: sqlite forbids sharing one across threads.
      conn = pipeline.open_db(cfg)
      try:
        pipeline.ingest(
          url,
          cfg,
          limit=request.limit,
          conn=conn,
          progress=state,
          log_buffer=job_log,
        )
      except Exception as exc:  # noqa: BLE001 - surface it, never kill the thread
        state.error = str(exc)[:300]
        state.finished = True
        job_log.add(str(exc)[:300], "error")
      finally:
        conn.close()

    threading.Thread(target=run, daemon=True, name="ingest").start()
    return {"ok": True, "url": url}

  @app.get("/api/ingest/status")
  def ingest_status() -> dict:
    """State of the current or most recent ingest."""
    state = job.get("current")
    if state is None:
      return {"running": False}
    return {"running": not state.finished, **state.as_dict()}

  @app.get("/api/ingest/log")
  def ingest_log(since: int = -1) -> dict:
    """Lines newer than `since`, so a one-second poll stays cheap."""
    lines, cursor = job_log.since(since)
    return {"lines": lines, "cursor": cursor}

  # The question currently on screen, by option index. The client posts back an
  # index, never a source name: nothing that identifies a source is ever sent
  # to the browser, so the comparison stays blind even to "view source"
  # (SPEC.md §9).
  pending: dict[str, elicit.Question] = {}

  @app.get("/api/elicit/next")
  def elicit_next() -> dict:
    """Serve the next blind comparison, from the least-answered cell."""
    conn = connect()
    question = elicit.next_question(conn)
    state = elicit.progress(conn)
    if question is None:
      pending.pop("q", None)
      return {"done": True, "progress": state.__dict__}
    pending["q"] = question
    return {
      "done": False,
      "track_id": question.track_id,
      "field": question.field,
      "family": question.family,
      "context": {"title": question.title, "artist": question.artist},
      "options": [
        {"value": option.value, "extra": [list(e) for e in option.extra]}
        for option in question.options
      ],
      "progress": state.__dict__,
    }

  @app.post("/api/elicit/choice")
  def elicit_choice(choice: Choice) -> dict:
    """Record an answer. `option` of -1 means the values looked equivalent."""
    question = pending.get("q")
    if question is None:
      raise HTTPException(status_code=409, detail="no question in flight")
    if choice.option >= len(question.options):
      raise HTTPException(status_code=400, detail="no such option")
    chosen = question.options[choice.option].sources if choice.option >= 0 else ()
    conn = connect()
    elicit.record(conn, question, chosen)
    pending.pop("q", None)
    return {"ok": True}

  @app.get("/api/elicit/progress")
  def elicit_progress() -> dict:
    """Counts for the session, and the cells calibrated so far."""
    conn = connect()
    derived = elicit.derive(elicit.observations(conn))
    return {
      "progress": elicit.progress(conn).__dict__,
      "cells": [
        {
          "family": family,
          "field": field_name,
          "answers": count,
          "ranking": list(derived.get((family, field_name), ())),
        }
        for (family, field_name), count in sorted(elicit.cell_counts(conn).items())
      ],
    }

  @app.post("/api/elicit/apply")
  def elicit_apply() -> dict:
    """Write the derived rankings into `precedence`.

    Re-arbitration is deliberately *not* triggered here. Rewriting every
    published file is a separate, explicit step (`music retag`), and it should
    not happen as a side effect of answering a question.
    """
    conn = connect()
    written = elicit.apply(conn)
    return {"ok": True, "cells": written}

  @app.post("/api/track/{track_id}/source")
  def choose_source(track_id: int, edit: FieldEdit) -> dict:
    """Adopt a specific source's value for a field, recorded as a manual choice."""
    conn = connect()
    persist(conn, track_id, [Decision(edit.field, edit.value, edit.value, "manual")])
    conn.execute(
      "UPDATE resolved_field SET source=?, decided_by='manual'"
      " WHERE track_id=? AND field=?",
      (edit.value, track_id, edit.field),
    )
    return {"ok": True}

  return app
