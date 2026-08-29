"""Tests for the CLI surface.

These check the commands wire up and behave, not that typer works.
"""

import pathlib

import pytest
from typer import testing

from tests.unit import conftest

from music_match import __version__
from music_match.cli import main
from music_match.db import connection
from music_match.db import queries
from music_match.tagging import fields
from music_match.tagging import fingerprint as fp
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


def build_source_tree(tmp_path: pathlib.Path,
                      names: tuple[str, ...]) -> pathlib.Path:
  """Creates a source folder of placeholder audio files and its config.

  Args:
    tmp_path: The temporary directory to build under.
    names: File names to create inside the source folder.

  Returns:
    Path to the written sources.toml.
  """
  folder = tmp_path / "yt-dlp"
  folder.mkdir(parents=True, exist_ok=True)
  for name in names:
    # Real audio, because dedup probes each file's quality and skips
    # anything mutagen cannot read.
    conftest.write_m4a(folder / name)
  config = tmp_path / "sources.toml"
  config.write_text(
      f'[sources.yt-dlp]\npath = "{folder}"\n\n'
      f'[duplicates]\npath = "{tmp_path / "dupes"}"\n',
      encoding="utf-8")
  return config


def fake_fingerprints(monkeypatch: pytest.MonkeyPatch,
                      by_name: dict[str, tuple[int, ...]],
                      duration: float = 180.0) -> None:
  """Replaces fpcalc with a lookup table, keeping unit tests hermetic.

  Args:
    monkeypatch: pytest's patching fixture.
    by_name: File name to the raw sub-fingerprints it should produce.
    duration: Duration to report for every file.
  """

  def fake(path: pathlib.Path, **_: object) -> fp.Fingerprint:
    return fp.Fingerprint(values=by_name[path.name], duration=duration)

  monkeypatch.setattr(fp, "fingerprint_file", fake)
  monkeypatch.setattr(fp, "have_fpcalc", lambda: True)


BASE_VALUES = tuple(index * 7919 for index in range(300))
OTHER_VALUES = tuple(0xAAAAAAAA ^ (index << 8) for index in range(300))


def test_scan_dry_run_writes_nothing(tmp_path: pathlib.Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
  """`scan --dry-run` reports the work without recording any of it."""
  config = build_source_tree(tmp_path, ("a.m4a", "b.m4a"))
  fake_fingerprints(monkeypatch, {"a.m4a": BASE_VALUES, "b.m4a": OTHER_VALUES})
  db_path = tmp_path / "state.db"

  result = runner.invoke(
      main.app,
      ["scan", "--sources",
       str(config), "--db",
       str(db_path), "--dry-run"])
  assert result.exit_code == 0
  assert "would fingerprint 2 files" in result.stdout
  with connection.open_db(db_path) as conn:
    assert queries.count_tracks(conn) == 0


def test_scan_records_fingerprints(tmp_path: pathlib.Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
  """A scan indexes every file it fingerprints."""
  config = build_source_tree(tmp_path, ("a.m4a", "b.m4a"))
  fake_fingerprints(monkeypatch, {"a.m4a": BASE_VALUES, "b.m4a": OTHER_VALUES})
  db_path = tmp_path / "state.db"

  result = runner.invoke(
      main.app, ["scan", "--sources",
                 str(config), "--db",
                 str(db_path)])
  assert result.exit_code == 0
  assert "fingerprinted 2, failed 0" in result.stdout
  with connection.open_db(db_path) as conn:
    assert queries.count_tracks(conn) == 2


def test_scan_is_resumable(tmp_path: pathlib.Path,
                           monkeypatch: pytest.MonkeyPatch) -> None:
  """A second scan skips what the first already did."""
  config = build_source_tree(tmp_path, ("a.m4a", "b.m4a"))
  fake_fingerprints(monkeypatch, {"a.m4a": BASE_VALUES, "b.m4a": OTHER_VALUES})
  db_path = tmp_path / "state.db"
  args = ["scan", "--sources", str(config), "--db", str(db_path)]

  runner.invoke(main.app, args)
  result = runner.invoke(main.app, args)
  assert "2 already fingerprinted, 0 to do" in result.stdout


def test_scan_limit_does_not_inflate_the_done_count(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """Files deferred by --limit are not reported as already fingerprinted."""
  config = build_source_tree(tmp_path, ("a.m4a", "b.m4a", "c.m4a"))
  fake_fingerprints(monkeypatch, {
      "a.m4a": BASE_VALUES,
      "b.m4a": OTHER_VALUES,
      "c.m4a": OTHER_VALUES
  })
  result = runner.invoke(main.app, [
      "scan", "--sources",
      str(config), "--db",
      str(tmp_path / "state.db"), "--limit", "1"
  ])
  assert "0 already fingerprinted, 1 to do" in result.stdout


def test_scan_survives_an_unreadable_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """One bad file does not abort a long run."""
  config = build_source_tree(tmp_path, ("good.m4a", "bad.m4a"))

  def fake(path: pathlib.Path, **_: object) -> fp.Fingerprint:
    if path.name == "bad.m4a":
      raise fp.FingerprintError("fpcalc found no audio in bad.m4a")
    return fp.Fingerprint(values=BASE_VALUES, duration=180.0)

  monkeypatch.setattr(fp, "fingerprint_file", fake)
  monkeypatch.setattr(fp, "have_fpcalc", lambda: True)

  result = runner.invoke(
      main.app,
      ["scan", "--sources",
       str(config), "--db",
       str(tmp_path / "state.db")])
  assert result.exit_code == 0
  assert "fingerprinted 1, failed 1" in result.stdout


def test_scan_reports_missing_fpcalc(tmp_path: pathlib.Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
  """Without fpcalc the command explains itself instead of crashing."""
  config = build_source_tree(tmp_path, ("a.m4a",))
  monkeypatch.setattr(fp, "have_fpcalc", lambda: False)
  result = runner.invoke(
      main.app,
      ["scan", "--sources",
       str(config), "--db",
       str(tmp_path / "s.db")])
  assert result.exit_code == 1


def test_scan_rejects_an_unknown_source(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """Naming a folder that is not configured exits with a message."""
  config = build_source_tree(tmp_path, ("a.m4a",))
  fake_fingerprints(monkeypatch, {"a.m4a": BASE_VALUES})
  result = runner.invoke(main.app, [
      "scan", "--sources",
      str(config), "--db",
      str(tmp_path / "s.db"), "--source", "nope"
  ])
  assert result.exit_code == 1


def scan_then_dedup(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
                    names: tuple[str, ...], values: dict[str, tuple[int, ...]],
                    dedup_args: list[str]) -> tuple[pathlib.Path, object]:
  """Scans a built library then runs dedup over it.

  Args:
    tmp_path: The temporary directory.
    monkeypatch: pytest's patching fixture.
    names: File names to create.
    values: File name to its fingerprint values.
    dedup_args: Extra arguments for the dedup command.

  Returns:
    The sources.toml path and the dedup command's result.
  """
  config = build_source_tree(tmp_path, names)
  fake_fingerprints(monkeypatch, values)
  db_path = tmp_path / "state.db"
  runner.invoke(main.app,
                ["scan", "--sources",
                 str(config), "--db",
                 str(db_path)])
  result = runner.invoke(
      main.app, ["dedup", "--sources",
                 str(config), "--db",
                 str(db_path)] + dedup_args)
  return config, result


def test_dedup_reports_without_moving(tmp_path: pathlib.Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
  """Without --apply, dedup reports and leaves every file in place."""
  _, result = scan_then_dedup(tmp_path, monkeypatch, ("a.m4a", "b.m4a"), {
      "a.m4a": BASE_VALUES,
      "b.m4a": BASE_VALUES
  }, [])
  assert result.exit_code == 0
  assert "1 duplicated recordings" in result.stdout
  assert "Re-run with --apply" in result.stdout
  assert (tmp_path / "yt-dlp" / "a.m4a").exists()
  assert (tmp_path / "yt-dlp" / "b.m4a").exists()


def test_dedup_apply_moves_the_loser(tmp_path: pathlib.Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
  """With --apply the duplicate is moved out, never deleted."""
  _, result = scan_then_dedup(tmp_path, monkeypatch, ("a.m4a", "b.m4a"), {
      "a.m4a": BASE_VALUES,
      "b.m4a": BASE_VALUES
  }, ["--apply"])
  assert result.exit_code == 0
  survivors = sorted(p.name for p in (tmp_path / "yt-dlp").glob("*.m4a"))
  moved = sorted(p.name for p in (tmp_path / "dupes").rglob("*.m4a"))
  # Both copies are identical here, so the keeper is decided by the path
  # tiebreak; what matters is that exactly one stayed and one moved, and
  # that nothing was deleted.
  assert len(survivors) == 1
  assert len(moved) == 1
  assert sorted(survivors + moved) == ["a.m4a", "b.m4a"]


def test_dedup_reports_nothing_when_there_are_no_duplicates(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """Distinct recordings produce no groups."""
  _, result = scan_then_dedup(tmp_path, monkeypatch, ("a.m4a", "b.m4a"), {
      "a.m4a": BASE_VALUES,
      "b.m4a": OTHER_VALUES
  }, [])
  assert "no duplicates found" in result.stdout


def test_dedup_needs_a_scan_first(tmp_path: pathlib.Path) -> None:
  """Running dedup on an empty index says what to do about it."""
  config = build_source_tree(tmp_path, ("a.m4a",))
  result = runner.invoke(
      main.app,
      ["dedup", "--sources",
       str(config), "--db",
       str(tmp_path / "state.db")])
  assert result.exit_code == 0
  assert "run `music-match scan` first" in result.stdout
