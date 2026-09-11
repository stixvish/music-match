"""HTTP surface for the review and editing UI (SPEC.md §15)."""

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from music import config, db
from music.arbitrate import Decision, persist
from music.publish import retag
from music.sources.url_override import parse as parse_url

STATIC = Path(__file__).parent / "static"


class FieldEdit(BaseModel):
  """A manual override of one field."""

  field: str
  value: str


class UrlOverride(BaseModel):
  """An authoritative link pasted by the user."""

  url: str


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
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))

  @app.get("/app.css")
  def stylesheet() -> FileResponse:
    return FileResponse(STATIC / "app.css", media_type="text/css")

  @app.get("/app.js")
  def script() -> FileResponse:
    return FileResponse(STATIC / "app.js", media_type="text/javascript")

  @app.get("/api/tracks")
  def tracks(status: str | None = None) -> list[dict]:
    """List tracks, newest first, optionally filtered by status."""
    conn = connect()
    where = "WHERE t.status = ?" if status else ""
    args = (status,) if status else ()
    return _rows(
      conn,
      "SELECT t.id, t.status, t.stage, t.genre_family, t.identity_confidence,"
      " t.is_video_rip, t.published_path, q.reason,"
      " (SELECT value FROM resolved_field"
      "  WHERE track_id=t.id AND field='artist') artist,"
      " (SELECT value FROM resolved_field"
      "  WHERE track_id=t.id AND field='title') title,"
      " s.channel, s.duration_s"
      " FROM track t"
      " JOIN source_file s ON s.id = t.source_file_id"
      " LEFT JOIN review_queue q ON q.track_id = t.id"
      f" {where} ORDER BY t.id DESC",
      *args,
    )

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
    return {
      "track": dict(head),
      "resolved": _rows(
        conn,
        "SELECT field, value, source, decided_by FROM resolved_field"
        " WHERE track_id=? ORDER BY field",
        track_id,
      ),
      "candidates": _rows(
        conn,
        "SELECT field, value, source, confidence FROM field_candidate"
        " WHERE track_id=? ORDER BY field, source",
        track_id,
      ),
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
    """Clear a track from the review queue and re-tag it if published."""
    conn = connect()
    conn.execute(
      "UPDATE review_queue SET resolved_at = datetime('now') WHERE track_id = ?",
      (track_id,),
    )
    conn.execute("DELETE FROM review_queue WHERE track_id = ?", (track_id,))
    conn.execute(
      "UPDATE track SET status='auto', updated_at=datetime('now') WHERE id=?",
      (track_id,),
    )
    path = retag(conn, track_id)
    return {"ok": True, "retagged": str(path) if path else None}

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
    """Serve the audio so a reviewer can hear the track (SPEC.md §15)."""
    conn = connect()
    row = conn.execute(
      "SELECT t.published_path, s.staging_path FROM track t"
      " JOIN source_file s ON s.id = t.source_file_id WHERE t.id = ?",
      (track_id,),
    ).fetchone()
    if row is None:
      raise HTTPException(status_code=404, detail="no such track")
    for candidate in (row["published_path"], row["staging_path"]):
      if candidate and Path(candidate).exists():
        return FileResponse(candidate)
    raise HTTPException(status_code=404, detail="audio file missing")

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
