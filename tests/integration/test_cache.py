"""Response cache (tasks/todo.md t10)."""

import pytest

from music import db
from music.sources import cache


@pytest.fixture
def conn(tmp_path):
  c = db.connect(tmp_path / "c.db")
  db.migrate(c)
  return c


def test_miss_then_hit(conn):
  calls = []

  def fetch():
    calls.append(1)
    return {"value": 42}

  first = cache.cached(conn, "musicbrainz", ("artist", "title"), fetch)
  second = cache.cached(conn, "musicbrainz", ("artist", "title"), fetch)
  assert first == second == {"value": 42}
  # the whole point: a rerun must not touch the network (SPEC.md §13)
  assert len(calls) == 1


def test_different_requests_do_not_collide(conn):
  cache.cached(conn, "mb", ("a",), lambda: 1)
  assert cache.cached(conn, "mb", ("b",), lambda: 2) == 2


def test_same_parts_different_source_do_not_collide(conn):
  cache.cached(conn, "mb", ("a",), lambda: 1)
  assert cache.cached(conn, "discogs", ("a",), lambda: 9) == 9
