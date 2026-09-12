"""Smoke test: proves the toolchain runs end to end (tasks/todo.md t1)."""

from music import cli


def test_parser_builds():
  parser = cli.build_parser()
  args = parser.parse_args(["ingest", "https://example.com/x", "--limit", "3"])
  assert args.limit == 3
  assert args.func is cli.cmd_ingest


def test_subcommand_is_required():
  import pytest

  with pytest.raises(SystemExit):
    cli.build_parser().parse_args([])


def test_serve_reloads_by_default():
  """A local tool edited while it runs must not hold stale code.

  The review ui saved artwork correctly, reported success, and never changed
  the file — because the running server still held the previous `retag` in
  memory. Nothing about that is visible from the browser.
  """
  args = cli.build_parser().parse_args(["serve"])
  assert args.no_reload is False
  assert cli.build_parser().parse_args(["serve", "--no-reload"]).no_reload is True
