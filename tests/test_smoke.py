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
