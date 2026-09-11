"""HTTP surface for the review and editing UI (SPEC.md §15)."""

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from music import config, db
from music.arbitrate import Decision, arbitrate, persist
from music.publish import publish_track, retag, tag
from music.sources.base import FieldCandidate
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
      " t.norm_artist, t.norm_title, s.channel, s.duration_s"
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
