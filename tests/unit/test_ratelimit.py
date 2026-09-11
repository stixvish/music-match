"""Rate limiting and backoff (tasks/todo.md t10)."""

import pytest

from music.sources.ratelimit import RateLimiter, with_backoff


def test_backoff_returns_on_first_success():
  calls = []
  assert with_backoff(lambda: calls.append(1) or "ok", sleep=lambda _: None) == "ok"
  assert len(calls) == 1


def test_backoff_retries_then_succeeds():
  state = {"n": 0}

  def flaky():
    state["n"] += 1
    if state["n"] < 3:
      raise RuntimeError("503")
    return "ok"

  assert with_backoff(flaky, sleep=lambda _: None) == "ok"
  assert state["n"] == 3


def test_backoff_gives_up_and_reraises():
  def always_fails():
    raise RuntimeError("still 503")

  with pytest.raises(RuntimeError, match="still 503"):
    with_backoff(always_fails, attempts=3, sleep=lambda _: None)


def test_backoff_delays_grow_exponentially():
  delays = []

  def always_fails():
    raise RuntimeError("x")

  with pytest.raises(RuntimeError):
    with_backoff(always_fails, attempts=4, base_delay=1.0, sleep=delays.append)
  # 3 sleeps for 4 attempts; each roughly double the last, plus jitter
  assert len(delays) == 3
  assert delays[0] < delays[1] < delays[2]


def test_rate_limiter_spaces_calls():
  limiter = RateLimiter(per_second=100.0)
  import time

  start = time.monotonic()
  for _ in range(3):
    limiter.wait()
  # 3 calls at 100/s must take at least ~2 intervals
  assert time.monotonic() - start >= 0.015
