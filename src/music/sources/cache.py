"""Persistent HTTP response cache.

Resolution is re-run many times while precedence is tuned; reruns must cost
nothing (SPEC.md §13).
"""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from typing import Any


def cache_key(source: str, *parts: object) -> str:
  """Build a stable key for a request.

  Args:
    source: Adapter name.
    *parts: Anything identifying the request.

  Returns:
    A hex key.
  """
  raw = "|".join([source, *(str(p) for p in parts)])
  return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(conn: sqlite3.Connection, key: str) -> Any | None:
  """Read a cached response.

  Args:
    conn: Open connection.
    key: Cache key.

  Returns:
    The decoded response, or None on a miss.
  """
  row = conn.execute("SELECT response FROM api_cache WHERE key = ?", (key,)).fetchone()
  return json.loads(row["response"]) if row else None


def put(conn: sqlite3.Connection, key: str, source: str, value: Any) -> None:
  """Store a response.

  Args:
    conn: Open connection.
    key: Cache key.
    source: Adapter name, for diagnostics.
    value: JSON-serialisable response.
  """
  conn.execute(
    "INSERT OR REPLACE INTO api_cache (key, source, response) VALUES (?,?,?)",
    (key, source, json.dumps(value)),
  )


def cached(
  conn: sqlite3.Connection,
  source: str,
  parts: tuple[object, ...],
  fetch: Callable[[], Any],
) -> Any:
  """Return a cached response, fetching and storing it on a miss.

  Args:
    conn: Open connection.
    source: Adapter name.
    parts: Request identity.
    fetch: Called only on a miss.

  Returns:
    The response.
  """
  key = cache_key(source, *parts)
  hit = get(conn, key)
  if hit is not None:
    return hit
  value = fetch()
  put(conn, key, source, value)
  return value
