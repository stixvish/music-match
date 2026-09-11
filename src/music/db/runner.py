"""Resumable stage runner.

Each track carries a `stage` marker, so a crash at track 1,800 of 2,329 resumes
at 1,800 rather than restarting (SPEC.md §13).
"""

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ordered pipeline stages (SPEC.md §6). a track advances one step at a time.
STAGES: tuple[str, ...] = (
  "acquired",
  "normalised",
  "classified",
  "resolved",
  "arbitrated",
  "transcoded",
  "tagged",
  "published",
)

StageFn = Callable[[sqlite3.Connection, sqlite3.Row], None]


@dataclass
class RunReport:
  """Outcome of a single run."""

  advanced: int = 0
  failed: int = 0
  skipped: int = 0

  def __str__(self) -> str:
    """Return a one-line summary for logs."""
    return f"advanced={self.advanced} failed={self.failed} skipped={self.skipped}"


def next_stage(stage: str) -> str | None:
  """Return the stage after `stage`, or None if it is terminal.

  Args:
    stage: Current stage name.

  Returns:
    The following stage, or None at the end of the pipeline.

  Raises:
    ValueError: If `stage` is not a known stage.
  """
  if stage not in STAGES:
    raise ValueError(f"unknown stage: {stage}")
  index = STAGES.index(stage)
  return STAGES[index + 1] if index + 1 < len(STAGES) else None


def pending(conn: sqlite3.Connection, stage: str) -> list[sqlite3.Row]:
  """Tracks sitting at `stage` and eligible to advance.

  Args:
    conn: Open connection.
    stage: Stage to select.

  Returns:
    Track rows in id order.
  """
  return conn.execute(
    "SELECT * FROM track WHERE stage = ? AND status NOT IN ('failed','skipped')"
    " ORDER BY id",
    (stage,),
  ).fetchall()


def advance(
  conn: sqlite3.Connection,
  stage: str,
  fn: StageFn,
  limit: int | None = None,
) -> RunReport:
  """Run one stage over every track waiting at it.

  Each track is committed independently: a failure marks that track and leaves
  the rest of the run intact, which is what makes the pipeline resumable.

  Args:
    conn: Open connection.
    stage: Stage to process.
    fn: Work to perform for a single track.
    limit: Stop after this many tracks, for testing and dry runs.

  Returns:
    A RunReport.
  """
  target = next_stage(stage)
  report = RunReport()
  rows = pending(conn, stage)
  if limit is not None:
    rows = rows[:limit]

  for row in rows:
    try:
      fn(conn, row)
    except Exception as exc:  # noqa: BLE001 - one bad track must not stop the run
      log.warning("track %s failed at %s: %s", row["id"], stage, exc)
      conn.execute(
        "UPDATE track SET status='failed', error=?, updated_at=datetime('now')"
        " WHERE id=?",
        (str(exc)[:500], row["id"]),
      )
      report.failed += 1
      continue

    if target is not None:
      conn.execute(
        "UPDATE track SET stage=?, updated_at=datetime('now') WHERE id=?",
        (target, row["id"]),
      )
    report.advanced += 1
  return report


def run_all(
  conn: sqlite3.Connection,
  handlers: dict[str, StageFn],
  stages: Sequence[str] = STAGES,
) -> dict[str, RunReport]:
  """Drive every stage in order.

  Args:
    conn: Open connection.
    handlers: Stage name to handler. Stages without a handler are skipped.
    stages: Stage order, overridable for testing.

  Returns:
    Per-stage reports.
  """
  reports: dict[str, RunReport] = {}
  for stage in stages:
    if stage not in handlers:
      continue
    reports[stage] = advance(conn, stage, handlers[stage])
    log.info("stage %s: %s", stage, reports[stage])
  return reports
