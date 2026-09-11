"""Smoke test: proves the toolchain runs end to end (tasks/todo.md t1)."""

from music import cli


def test_cli_is_callable():
  assert cli.main() == 0
