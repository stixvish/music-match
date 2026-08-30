"""Tests for the shared HTTP layer.

Rate limiting, backoff and caching are the parts that decide whether a
pass over thousands of tracks gets throttled or blocked, so they are
tested directly rather than through a source adapter.
"""

import pathlib
from typing import Any

import pytest
import requests

from music_match.sources import http


class FakeResponse:
  """A minimal stand-in for requests.Response."""

  def __init__(self,
               status_code: int = 200,
               body: Any = None,
               headers: dict[str, str] | None = None) -> None:
    """Records the response to return.

    Args:
      status_code: The HTTP status.
      body: The JSON body.
      headers: Response headers.
    """
    self.status_code = status_code
    self._body = body if body is not None else {"ok": True}
    self.headers = headers or {}

  def json(self) -> Any:
    """Returns the JSON body.

    Returns:
      The body.

    Raises:
      ValueError: If the body was set to the marker for invalid JSON.
    """
    if self._body == "NOT JSON":
      raise ValueError("no json")
    return self._body


class FakeSession:
  """Returns queued responses and records the requests made."""

  def __init__(self, responses: list[Any]) -> None:
    """Queues responses.

    Args:
      responses: Responses or exceptions, returned in order.
    """
    self._responses = responses
    self.requests: list[tuple[str, str]] = []

  def request(self, method: str, url: str, **_: Any) -> FakeResponse:
    """Returns the next queued response.

    Args:
      method: The HTTP method.
      url: The URL.

    Returns:
      The next response.

    Raises:
      Exception: If the next queued item is an exception.
    """
    self.requests.append((method, url))
    index = min(len(self.requests) - 1, len(self._responses) - 1)
    item = self._responses[index]
    if isinstance(item, Exception):
      raise item
    return item


def client_with(responses: list[Any],
                **kwargs: Any) -> tuple[http.HttpClient, FakeSession]:
  """Builds a client wired to a fake session.

  Args:
    responses: Responses to queue.
    **kwargs: Passed to HttpClient.

  Returns:
    The client and its session.
  """
  client = http.HttpClient(user_agent="test", **kwargs)
  session = FakeSession(responses)
  client._session = session  # pylint: disable=protected-access
  return client, session


def test_get_json_returns_the_body() -> None:
  """A successful request yields the parsed body."""
  client, _ = client_with([FakeResponse(body={"a": 1})])
  assert client.get_json("https://example.com") == {"a": 1}


def test_client_sends_its_user_agent() -> None:
  """MusicBrainz requires one, so it is never optional."""
  client, session = client_with([FakeResponse()])
  client.get_json("https://example.com")
  assert session.requests == [("GET", "https://example.com")]


def test_a_client_error_is_not_retried() -> None:
  """A 404 is an answer, not a hiccup."""
  client, session = client_with([FakeResponse(status_code=404)])
  with pytest.raises(http.HttpError, match="404"):
    client.get_json("https://example.com")
  assert len(session.requests) == 1


def test_rate_limited_requests_are_retried(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """A 429 backs off and tries again rather than failing the run."""
  monkeypatch.setattr(http.time, "sleep", lambda _: None)
  client, session = client_with(
      [FakeResponse(status_code=429),
       FakeResponse(body={"ok": 1})])
  assert client.get_json("https://example.com") == {"ok": 1}
  assert len(session.requests) == 2


def test_server_errors_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
  """A 503 is transient and worth another attempt."""
  monkeypatch.setattr(http.time, "sleep", lambda _: None)
  client, session = client_with(
      [FakeResponse(status_code=503),
       FakeResponse(body={"ok": 1})])
  assert client.get_json("https://example.com") == {"ok": 1}
  assert len(session.requests) == 2


def test_retries_eventually_give_up(monkeypatch: pytest.MonkeyPatch) -> None:
  """Retrying forever would hang a run, so attempts are bounded."""
  monkeypatch.setattr(http.time, "sleep", lambda _: None)
  client, session = client_with([FakeResponse(status_code=503)], max_attempts=3)
  with pytest.raises(http.HttpError, match="after 3 attempts"):
    client.get_json("https://example.com")
  assert len(session.requests) == 3


def test_network_errors_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
  """A dropped connection is retried like any other transient failure."""
  monkeypatch.setattr(http.time, "sleep", lambda _: None)
  client, session = client_with(
      [requests.ConnectionError("dropped"),
       FakeResponse(body={"ok": 1})])
  assert client.get_json("https://example.com") == {"ok": 1}
  assert len(session.requests) == 2


def test_retry_after_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
  """When a server says how long to wait, wait that long."""
  waits: list[float] = []
  monkeypatch.setattr(http.time, "sleep", waits.append)
  client, _ = client_with([
      FakeResponse(status_code=429, headers={"Retry-After": "5"}),
      FakeResponse(body={"ok": 1}),
  ])
  client.get_json("https://example.com")
  assert 5.0 in waits


def test_an_absurd_retry_after_is_capped(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """A misconfigured header must not stall the run for an hour."""
  waits: list[float] = []
  monkeypatch.setattr(http.time, "sleep", waits.append)
  client, _ = client_with([
      FakeResponse(status_code=429, headers={"Retry-After": "99999"}),
      FakeResponse(body={"ok": 1}),
  ])
  client.get_json("https://example.com")
  assert max(waits) <= http.MAX_RETRY_WAIT_SECONDS


def test_non_json_is_an_error() -> None:
  """A body that is not JSON is reported, not returned as garbage."""
  client, _ = client_with([FakeResponse(body="NOT JSON")])
  with pytest.raises(http.HttpError, match="did not return JSON"):
    client.get_json("https://example.com")


def test_rate_limit_waits_between_requests(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """The minimum interval is enforced here, not trusted to call sites."""
  waits: list[float] = []
  monkeypatch.setattr(http.time, "sleep", waits.append)
  monkeypatch.setattr(http.time, "monotonic", lambda: 100.0)
  client, _ = client_with([FakeResponse()], min_interval_seconds=1.0)
  client.get_json("https://example.com/a")
  client.get_json("https://example.com/b")
  assert waits and waits[-1] == pytest.approx(1.0)


def test_no_wait_when_unthrottled(monkeypatch: pytest.MonkeyPatch) -> None:
  """A source with no limit does not pay for one."""
  waits: list[float] = []
  monkeypatch.setattr(http.time, "sleep", waits.append)
  client, _ = client_with([FakeResponse()], min_interval_seconds=0.0)
  client.get_json("https://example.com/a")
  client.get_json("https://example.com/b")
  assert not waits


def test_cache_serves_a_repeat_request(tmp_path: pathlib.Path) -> None:
  """Re-running a probe should not re-query four APIs."""
  cache = http.ResponseCache(tmp_path, ttl_seconds=1000)
  client, session = client_with([FakeResponse(body={"v": 1})], cache=cache)
  first = client.get_json("https://example.com", params={"q": "x"})
  second = client.get_json("https://example.com", params={"q": "x"})
  assert first == second == {"v": 1}
  assert len(session.requests) == 1


def test_cache_distinguishes_parameters(tmp_path: pathlib.Path) -> None:
  """Different queries are different cache entries."""
  cache = http.ResponseCache(tmp_path, ttl_seconds=1000)
  client, session = client_with([FakeResponse()], cache=cache)
  client.get_json("https://example.com", params={"q": "a"})
  client.get_json("https://example.com", params={"q": "b"})
  assert len(session.requests) == 2


def test_cache_expires(tmp_path: pathlib.Path) -> None:
  """A stale entry is re-fetched, so the probe can see changes."""
  cache = http.ResponseCache(tmp_path, ttl_seconds=-1)
  client, session = client_with([FakeResponse()], cache=cache)
  client.get_json("https://example.com")
  client.get_json("https://example.com")
  assert len(session.requests) == 2


def test_cache_can_be_bypassed(tmp_path: pathlib.Path) -> None:
  """`use_cache=False` always hits the network."""
  cache = http.ResponseCache(tmp_path, ttl_seconds=1000)
  client, session = client_with([FakeResponse()], cache=cache)
  client.get_json("https://example.com")
  client.get_json("https://example.com", use_cache=False)
  assert len(session.requests) == 2


def test_unreadable_cache_is_ignored(tmp_path: pathlib.Path) -> None:
  """A corrupt cache file means a slower run, not a failed one."""
  cache = http.ResponseCache(tmp_path, ttl_seconds=1000)
  key = http.ResponseCache.key("https://example.com", None)
  (tmp_path / f"{key}.json").write_text("{not json", encoding="utf-8")
  assert cache.get(key) is None


def test_cache_key_is_order_independent() -> None:
  """The same query in a different dict order is the same request."""
  first = http.ResponseCache.key("https://x", {"a": 1, "b": 2})
  second = http.ResponseCache.key("https://x", {"b": 2, "a": 1})
  assert first == second
