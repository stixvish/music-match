"""Shared HTTP plumbing for the metadata sources.

Three things every source needs and none should reimplement:

- **Rate limiting.** MusicBrainz allows one request a second and means it;
  Discogs allows sixty a minute. A pass over 2000 tracks that ignores
  either gets throttled or blocked, so the interval is enforced here per
  client rather than trusted to call sites.
- **Backoff.** Retries on 429 and on the 5xx range, honouring
  `Retry-After` when the server sends one.
- **Caching.** The probe is meant to be run repeatedly while tuning
  precedence, and re-running it should not mean re-querying four APIs.
  Responses are cached on disk with a TTL and can be bypassed.
"""

import dataclasses
import hashlib
import json
import pathlib
import time
from typing import Any, Mapping

import requests

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_CACHE_DIR = pathlib.Path(".music-match/http-cache")
DEFAULT_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Cap on how long a server's Retry-After is honoured, so a
# misconfigured header cannot stall a run indefinitely.
MAX_RETRY_WAIT_SECONDS = 30.0


class HttpError(Exception):
  """Raised when a request cannot be completed."""


class ResponseCache:
  """A disk cache of JSON responses, keyed by request."""

  def __init__(self,
               directory: pathlib.Path = DEFAULT_CACHE_DIR,
               ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS) -> None:
    """Sets up the cache.

    Args:
      directory: Where cached responses are written.
      ttl_seconds: How long an entry stays usable.
    """
    self._directory = directory
    self._ttl = ttl_seconds

  @staticmethod
  def key(url: str, params: Mapping[str, Any] | None) -> str:
    """Builds a stable cache key for a request.

    Args:
      url: The request URL.
      params: The query parameters, if any.

    Returns:
      A hex digest identifying this request.
    """
    payload = json.dumps([url, sorted((params or {}).items())],
                         sort_keys=True,
                         default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

  def get(self, key: str) -> Any | None:
    """Reads a cached response.

    Args:
      key: The cache key.

    Returns:
      The cached JSON, or None if absent, expired, or unreadable.
    """
    path = self._directory / f"{key}.json"
    try:
      with path.open("rb") as handle:
        entry = json.load(handle)
    except (OSError, json.JSONDecodeError):
      return None
    stored_at = entry.get("stored_at", 0.0)
    if time.time() - stored_at > self._ttl:
      return None
    return entry.get("body")

  def put(self, key: str, body: Any) -> None:
    """Writes a response to the cache, ignoring failures.

    A cache that cannot be written is a slower run, not a failed one.

    Args:
      key: The cache key.
      body: The JSON body to store.
    """
    try:
      self._directory.mkdir(parents=True, exist_ok=True)
      path = self._directory / f"{key}.json"
      with path.open("w", encoding="utf-8") as handle:
        json.dump({"stored_at": time.time(), "body": body}, handle)
    except OSError:
      pass


@dataclasses.dataclass
class HttpClient:
  """A rate-limited, retrying, optionally caching JSON HTTP client.

  Attributes:
    user_agent: Sent on every request. MusicBrainz requires a meaningful
      one and will throttle a generic value.
    min_interval_seconds: Least time between two requests from this
      client.
    cache: Where to cache responses, or None to always hit the network.
    timeout_seconds: Per-request timeout.
    max_attempts: How many times to try before giving up.
  """
  user_agent: str
  min_interval_seconds: float = 0.0
  cache: ResponseCache | None = None
  timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
  max_attempts: int = DEFAULT_MAX_ATTEMPTS
  _last_request_at: float = dataclasses.field(default=0.0, init=False)
  _session: requests.Session | None = dataclasses.field(default=None,
                                                        init=False)

  def get_json(self,
               url: str,
               params: Mapping[str, Any] | None = None,
               headers: Mapping[str, str] | None = None,
               *,
               use_cache: bool = True) -> Any:
    """Fetches a URL and parses the JSON body.

    Args:
      url: The URL to fetch.
      params: Query parameters.
      headers: Extra headers, merged over the user agent.
      use_cache: Whether this request may be served from, and written to,
        the cache.

    Returns:
      The parsed JSON body.

    Raises:
      HttpError: If the request fails after every attempt, or the
        response is not JSON.
    """
    cache_key = ResponseCache.key(url, params)
    if self.cache is not None and use_cache:
      cached = self.cache.get(cache_key)
      if cached is not None:
        return cached

    response = self._request("GET", url, params=params, headers=headers)
    body = self._parse(response, url)
    if self.cache is not None and use_cache:
      self.cache.put(cache_key, body)
    return body

  def post_json(self,
                url: str,
                data: Mapping[str, str],
                headers: Mapping[str, str] | None = None) -> Any:
    """Posts a form body and parses the JSON response.

    Never cached: the only POST here is a token exchange.

    Args:
      url: The URL to post to.
      data: Form fields.
      headers: Extra headers, merged over the user agent.

    Returns:
      The parsed JSON body.

    Raises:
      HttpError: If the request fails after every attempt.
    """
    response = self._request("POST", url, data=data, headers=headers)
    return self._parse(response, url)

  def _parse(self, response: requests.Response, url: str) -> Any:
    """Parses a JSON response body.

    Args:
      response: The response to read.
      url: The URL it came from, for error messages.

    Returns:
      The parsed body.

    Raises:
      HttpError: If the body is not JSON.
    """
    try:
      return response.json()
    except ValueError as err:
      raise HttpError(f"{url} did not return JSON: {err}") from err

  def _request(self,
               method: str,
               url: str,
               params: Mapping[str, Any] | None = None,
               data: Mapping[str, str] | None = None,
               headers: Mapping[str, str] | None = None) -> requests.Response:
    """Makes a request, waiting out the rate limit and retrying failures.

    Args:
      method: The HTTP method.
      url: The URL.
      params: Query parameters.
      data: Form body, for POST.
      headers: Extra headers.

    Returns:
      A successful response.

    Raises:
      HttpError: If every attempt failed.
    """
    if self._session is None:
      self._session = requests.Session()
    merged = {"User-Agent": self.user_agent, **(headers or {})}

    last_error = ""
    for attempt in range(1, self.max_attempts + 1):
      self._wait_for_slot()
      try:
        response = self._session.request(method,
                                         url,
                                         params=params,
                                         data=data,
                                         headers=merged,
                                         timeout=self.timeout_seconds)
      except requests.RequestException as err:
        last_error = str(err)
      else:
        if response.status_code not in _RETRY_STATUSES:
          if response.status_code >= 400:
            raise HttpError(f"{url} returned HTTP {response.status_code}")
          return response
        last_error = f"HTTP {response.status_code}"
        if attempt < self.max_attempts:
          time.sleep(self._retry_delay(response, attempt))
        continue
      if attempt < self.max_attempts:
        time.sleep(min(2.0**attempt, MAX_RETRY_WAIT_SECONDS))

    raise HttpError(f"{url} failed after {self.max_attempts} attempts: "
                    f"{last_error}")

  def _retry_delay(self, response: requests.Response, attempt: int) -> float:
    """Decides how long to wait before retrying.

    Args:
      response: The response that asked to be retried.
      attempt: Which attempt just failed, counting from 1.

    Returns:
      Seconds to wait, honouring `Retry-After` when it is sane.
    """
    header = response.headers.get("Retry-After")
    if header:
      try:
        return min(float(header), MAX_RETRY_WAIT_SECONDS)
      except ValueError:
        pass
    return min(2.0**attempt, MAX_RETRY_WAIT_SECONDS)

  def _wait_for_slot(self) -> None:
    """Sleeps until this client is allowed to make another request."""
    if self.min_interval_seconds <= 0:
      return
    elapsed = time.monotonic() - self._last_request_at
    if elapsed < self.min_interval_seconds:
      time.sleep(self.min_interval_seconds - elapsed)
    self._last_request_at = time.monotonic()
