"""Tests for the CLI surface.

These check the commands wire up and behave, not that typer works.
"""

import pathlib

from typer import testing

from music_match import __version__
from music_match.cli import main
from music_match.tagging import fields
from music_match.tagging import tags as tag_io

runner = testing.CliRunner()


def test_version_prints_the_package_version() -> None:
  """`music-match version` reports the installed version."""
  result = runner.invoke(main.app, ["version"])
  assert result.exit_code == 0
  assert __version__ in result.stdout


def test_bare_invocation_shows_help() -> None:
  """Running with no arguments shows help rather than doing something."""
  assert "Usage" in runner.invoke(main.app, []).stdout


def test_config_show_lists_folders_and_precedence(
    tmp_path: pathlib.Path) -> None:
  """`config show` reports both files, including field overrides."""
  sources = tmp_path / "sources.toml"
  sources.write_text('[sources.yt-dlp]\npath = "~/Music/yt-dlp"\n',
                     encoding="utf-8")
  precedence = tmp_path / "precedence.toml"
  precedence.write_text(
      '[genres.default]\norder = ["musicbrainz"]\n\n'
      '[genres.electronic]\norder = ["discogs"]\n\n'
      '[genres.electronic.fields]\nremixer = ["discogs"]\n',
      encoding="utf-8")

  result = runner.invoke(main.app, [
      "config", "show", "--sources",
      str(sources), "--precedence",
      str(precedence)
  ])
  assert result.exit_code == 0
  assert "yt-dlp" in result.stdout
  assert "electronic: discogs" in result.stdout
  assert "remixer: discogs" in result.stdout


def test_config_show_reports_a_missing_file(tmp_path: pathlib.Path) -> None:
  """A missing config exits non-zero with a readable message."""
  result = runner.invoke(
      main.app, ["config", "show", "--sources",
                 str(tmp_path / "nope.toml")])
  assert result.exit_code == 1


def test_db_init_creates_the_database(tmp_path: pathlib.Path) -> None:
  """`db init` creates the file and reports the tables it made."""
  db_path = tmp_path / "music_match.db"
  result = runner.invoke(main.app, ["db", "init", "--db", str(db_path)])
  assert result.exit_code == 0
  assert db_path.exists()
  assert "tracks" in result.stdout


def test_db_init_dry_run_writes_nothing(tmp_path: pathlib.Path) -> None:
  """`db init --dry-run` reports the plan and leaves the disk alone."""
  db_path = tmp_path / "music_match.db"
  result = runner.invoke(
      main.app,
      ["db", "init", "--db", str(db_path), "--dry-run"])
  assert result.exit_code == 0
  assert not db_path.exists()
  assert "dry run" in result.stdout


def test_tags_show_prints_set_fields(m4a_file: pathlib.Path) -> None:
  """`tags show` lists the fields a file actually sets."""
  tag_io.write_tags(m4a_file, fields.TrackTags(title="Strobe",
                                               artist="deadmau5"))
  result = runner.invoke(main.app, ["tags", "show", str(m4a_file)])
  assert result.exit_code == 0
  assert "Strobe" in result.stdout
  assert "album" not in result.stdout


def test_tags_show_can_include_empty_fields(m4a_file: pathlib.Path) -> None:
  """`--show-empty` lists every field, including the unset ones."""
  result = runner.invoke(
      main.app, ["tags", "show", str(m4a_file), "--show-empty"])
  assert result.exit_code == 0
  assert "album_artist" in result.stdout


def test_tags_show_reports_an_unreadable_file(tmp_path: pathlib.Path) -> None:
  """A file mutagen cannot parse exits non-zero instead of traceback-ing."""
  path = tmp_path / "notes.txt"
  path.write_text("not audio", encoding="utf-8")
  result = runner.invoke(main.app, ["tags", "show", str(path)])
  assert result.exit_code == 1
