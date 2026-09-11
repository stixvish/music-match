"""Rate limiting and retry.

MusicBrainz allows 1 req/s and returns 503 rather than queuing — 9 of 30
requests failed before backoff was added (SPEC.md §4). Discogs uses a rolling
60-second window; AcoustID allows 3/s.
"""

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
  """Spaces calls out to at most `per_second`."""

  per_second: float
  _last: float = field(default=0.0, repr=False)

  def wait(self) -> None:
    """Block until the next call is allowed."""
    interval = 1.0 / self.per_second
    elapsed = time.monotonic() - self._last
    if elapsed < interval:
      time.sleep(interval - elapsed)
    self._last = time.monotonic()


def with_backoff[T](
  fn: Callable[[], T],
  *,
  attempts: int = 4,
  base_delay: float = 1.0,
  sleep: Callable[[float], None] = time.sleep,
) -> T:
  """Retry with exponential backoff and jitter.

  Jitter matters: without it, parallel workers that fail together retry
  together and keep colliding.

  Args:
    fn: Work to attempt.
    attempts: Total tries, including the first.
    base_delay: Seconds before the first retry.
    sleep: Injected for tests.

  Returns:
    Whatever `fn` returns.

  Raises:
    Exception: The last failure, if every attempt fails.
  """
  last: Exception | None = None
  for attempt in range(attempts):
    try:
      return fn()
    except Exception as exc:  # noqa: BLE001 - caller decides what is fatal
      last = exc
      if attempt == attempts - 1:
        break
      delay = base_delay * (2**attempt)
      sleep(delay + random.uniform(0, delay * 0.3))
  assert last is not None
  raise last
