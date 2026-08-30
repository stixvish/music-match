"""Tests for the CLI surface.

These check the commands wire up and behave, not that typer works.
"""

import pathlib

import pytest
from typer import testing

from tests.unit import conftest

from music_match import __version__
from music_match import probe as probe_lib
from music_match.cli import main
from music_match.db import connection
from music_match.db import queries
from music_match.sources import base as source_base
from music_match.sources import http
from music_match.tagging import fields
from music_match.tagging import fingerprint as fp
from music_match.tagging import genre as genre_lib
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


class FakeDetector:
  """Stands in for GenreDetector so the CLI is testable without Essentia."""

  def __init__(self, results: dict[str, str], top_n: int = 5) -> None:
    """Records what to return for each file name.

    Args:
      results: File name to the label to predict. A name mapped to the
        empty string produces no prediction at all.
      top_n: Accepted for signature compatibility; unused.
    """
    self._results = results
    self._top_n = top_n

  def detect(self, path: pathlib.Path) -> genre_lib.GenreResult:
    """Returns the configured result for a file.

    Args:
      path: The file being analysed.

    Returns:
      The configured prediction.

    Raises:
      GenreError: If the file was not configured, standing in for a file
        the model cannot decode.
    """
    if path.name not in self._results:
      raise genre_lib.GenreError(f"could not analyse {path}")
    label = self._results[path.name]
    if not label:
      return genre_lib.GenreResult(predictions=())
    return genre_lib.GenreResult(
        predictions=(genre_lib.Prediction(label=label, confidence=0.9),))


def fake_detector(monkeypatch: pytest.MonkeyPatch, results: dict[str,
                                                                 str]) -> None:
  """Replaces GenreDetector with a stub returning fixed predictions.

  Args:
    monkeypatch: pytest's patching fixture.
    results: File name to the label it should predict.
  """
  monkeypatch.setattr(genre_lib,
                      "GenreDetector",
                      lambda models, top_n=5: FakeDetector(results, top_n))


def test_genre_show_prints_predictions(tmp_path: pathlib.Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
  """`genre show` lists predictions and the precedence key they map to."""
  build_source_tree(tmp_path, ("a.m4a",))
  fake_detector(monkeypatch, {"a.m4a": "Electronic---Deep House"})
  result = runner.invoke(
      main.app,
      ["genre", "show", str(tmp_path / "yt-dlp" / "a.m4a")])
  assert result.exit_code == 0
  assert "Electronic---Deep House" in result.stdout
  assert "precedence key: electronic" in result.stdout


def test_genre_show_handles_no_prediction(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """A file too short to analyse says so rather than showing nothing."""
  build_source_tree(tmp_path, ("a.m4a",))
  fake_detector(monkeypatch, {"a.m4a": ""})
  result = runner.invoke(
      main.app,
      ["genre", "show", str(tmp_path / "yt-dlp" / "a.m4a")])
  assert result.exit_code == 0
  assert "too little audio" in result.stdout


def test_genre_show_reports_an_unreadable_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """A file the model cannot decode exits non-zero with a message."""
  build_source_tree(tmp_path, ("a.m4a",))
  fake_detector(monkeypatch, {})
  result = runner.invoke(
      main.app,
      ["genre", "show", str(tmp_path / "yt-dlp" / "a.m4a")])
  assert result.exit_code == 1


def test_genre_index_records_labels(tmp_path: pathlib.Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
  """`genre index` stores a detected genre for every file."""
  config = build_source_tree(tmp_path, ("a.m4a", "b.m4a"))
  fake_detector(monkeypatch, {
      "a.m4a": "Electronic---Deep House",
      "b.m4a": "Hip Hop---Trap"
  })
  db_path = tmp_path / "state.db"
  result = runner.invoke(
      main.app,
      ["genre", "index", "--sources",
       str(config), "--db",
       str(db_path)])
  assert result.exit_code == 0
  assert "analysed 2, failed 0" in result.stdout
  with connection.open_db(db_path) as conn:
    counts = {
        label: count for label, count, _ in queries.detected_genre_counts(conn)
    }
  assert counts == {"Electronic---Deep House": 1, "Hip Hop---Trap": 1}


def test_genre_index_dry_run_writes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """`genre index --dry-run` reports the work without recording it."""
  config = build_source_tree(tmp_path, ("a.m4a",))
  fake_detector(monkeypatch, {"a.m4a": "Rock---Indie Rock"})
  db_path = tmp_path / "state.db"
  result = runner.invoke(main.app, [
      "genre", "index", "--sources",
      str(config), "--db",
      str(db_path), "--dry-run"
  ])
  assert result.exit_code == 0
  assert "would analyse 1 files" in result.stdout
  with connection.open_db(db_path) as conn:
    assert queries.count_tracks(conn) == 0


def test_genre_index_is_resumable(tmp_path: pathlib.Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
  """A second run skips files that already have a genre."""
  config = build_source_tree(tmp_path, ("a.m4a",))
  fake_detector(monkeypatch, {"a.m4a": "Jazz---Bebop"})
  args = [
      "genre", "index", "--sources",
      str(config), "--db",
      str(tmp_path / "state.db")
  ]
  runner.invoke(main.app, args)
  result = runner.invoke(main.app, args)
  assert "1 already analysed, 0 to do" in result.stdout


def test_genre_index_survives_a_bad_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """One undecodable file does not abort the run."""
  config = build_source_tree(tmp_path, ("good.m4a", "bad.m4a"))
  fake_detector(monkeypatch, {"good.m4a": "Pop---Synth-pop"})
  result = runner.invoke(main.app, [
      "genre", "index", "--sources",
      str(config), "--db",
      str(tmp_path / "state.db")
  ])
  assert result.exit_code == 0
  assert "analysed 1, failed 1" in result.stdout


def test_genre_index_does_not_clear_fingerprints(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """The genre pass fills its own column without wiping the scan's.

  Both passes upsert the same row, so a non-coalescing update here would
  silently undo a full fingerprint scan.
  """
  config = build_source_tree(tmp_path, ("a.m4a",))
  fake_fingerprints(monkeypatch, {"a.m4a": BASE_VALUES})
  db_path = tmp_path / "state.db"
  runner.invoke(main.app,
                ["scan", "--sources",
                 str(config), "--db",
                 str(db_path)])
  fake_detector(monkeypatch, {"a.m4a": "Electronic---Techno"})
  runner.invoke(
      main.app,
      ["genre", "index", "--sources",
       str(config), "--db",
       str(db_path)])

  with connection.open_db(db_path) as conn:
    row = conn.execute(
        "SELECT fingerprint, detected_genre FROM tracks").fetchone()
  assert row["fingerprint"] is not None
  assert row["detected_genre"] == "Electronic---Techno"


def test_genre_summary_reports_counts(tmp_path: pathlib.Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
  """`genre summary` groups what the index recorded."""
  config = build_source_tree(tmp_path, ("a.m4a", "b.m4a"))
  fake_detector(monkeypatch, {
      "a.m4a": "Electronic---Techno",
      "b.m4a": "Electronic---Techno"
  })
  db_path = tmp_path / "state.db"
  runner.invoke(
      main.app,
      ["genre", "index", "--sources",
       str(config), "--db",
       str(db_path)])
  result = runner.invoke(main.app, ["genre", "summary", "--db", str(db_path)])
  assert "2 tracks across 1 labels" in result.stdout
  assert "Electronic---Techno" in result.stdout


def test_genre_summary_without_an_index(tmp_path: pathlib.Path) -> None:
  """With nothing recorded, the summary says what to run."""
  result = runner.invoke(
      main.app, ["genre", "summary", "--db",
                 str(tmp_path / "state.db")])
  assert "run `music-match genre index` first" in result.stdout


def test_fetch_models_is_idempotent(tmp_path: pathlib.Path) -> None:
  """With every model already present, nothing is downloaded."""
  for name in genre_lib.MODEL_URLS:
    (tmp_path / name).write_bytes(b"x")
  result = runner.invoke(main.app,
                         ["genre", "fetch-models", "--models",
                          str(tmp_path)])
  assert result.exit_code == 0
  assert "already present" in result.stdout


def test_fetch_models_dry_run_downloads_nothing(tmp_path: pathlib.Path) -> None:
  """`fetch-models --dry-run` lists the files without fetching them."""
  models = tmp_path / "models"
  result = runner.invoke(
      main.app, ["genre", "fetch-models", "--models",
                 str(models), "--dry-run"])
  assert result.exit_code == 0
  assert "would download 3 files" in result.stdout
  assert not models.exists()


def test_genre_index_stores_confidence(tmp_path: pathlib.Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
  """The label is stored with the score behind it, not on its own.

  A bare label is not enough to act on: measured against known tracks the
  model is right about a fifth of the time below 0.15 confidence.
  """
  config = build_source_tree(tmp_path, ("a.m4a",))
  fake_detector(monkeypatch, {"a.m4a": "Electronic---Techno"})
  db_path = tmp_path / "state.db"
  runner.invoke(
      main.app,
      ["genre", "index", "--sources",
       str(config), "--db",
       str(db_path)])
  with connection.open_db(db_path) as conn:
    row = conn.execute(
        "SELECT detected_genre, genre_confidence FROM tracks").fetchone()
  assert row["detected_genre"] == "Electronic---Techno"
  assert row["genre_confidence"] == pytest.approx(0.9)


def test_genre_summary_can_filter_by_confidence(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """`--min-confidence` hides labels the model barely backed."""
  config = build_source_tree(tmp_path, ("a.m4a",))
  fake_detector(monkeypatch, {"a.m4a": "Electronic---Techno"})
  db_path = tmp_path / "state.db"
  runner.invoke(
      main.app,
      ["genre", "index", "--sources",
       str(config), "--db",
       str(db_path)])

  kept = runner.invoke(
      main.app,
      ["genre", "summary", "--db",
       str(db_path), "--min-confidence", "0.5"])
  dropped = runner.invoke(
      main.app,
      ["genre", "summary", "--db",
       str(db_path), "--min-confidence", "0.95"])
  assert "Electronic---Techno" in kept.stdout
  assert "no genres detected yet" in dropped.stdout


def test_genre_index_survives_a_file_with_no_prediction(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """A track too short to analyse is skipped, not fatal.

  Files shorter than one analysis window embed to nothing. Letting that
  end the run would abandon a whole library pass at the first jingle.
  """
  config = build_source_tree(tmp_path, ("good.m4a", "tiny.m4a"))
  fake_detector(monkeypatch, {
      "good.m4a": "Electronic---Techno",
      "tiny.m4a": ""
  })
  result = runner.invoke(main.app, [
      "genre", "index", "--sources",
      str(config), "--db",
      str(tmp_path / "state.db")
  ])
  assert result.exit_code == 0
  assert "analysed 1, failed 1" in result.stdout


class ProbeStub(source_base.MetadataSource):
  """A source with a fixed answer, for testing the probe command."""

  def __init__(self, name: str, tags: fields.TrackTags | None) -> None:
    """Records the canned answer.

    Args:
      name: The source name.
      tags: The tags to return, or None for no candidates.
    """
    super().__init__(http.HttpClient(user_agent="test"))
    self.name = name
    self._tags = tags

  def is_available(self) -> bool:
    """Returns True; credentials are not part of these tests."""
    return True

  def search(self,
             query: source_base.SourceQuery,
             limit: int = 3) -> list[source_base.SourceResult]:
    """Returns the canned candidate.

    Args:
      query: Ignored.
      limit: Ignored.

    Returns:
      The canned candidate, or none.
    """
    del query, limit
    if self._tags is None:
      return []
    return [
        source_base.SourceResult(source=self.name,
                                 source_id="1",
                                 tags=self._tags)
    ]


def fake_sources(monkeypatch: pytest.MonkeyPatch,
                 answers: dict[str, fields.TrackTags | None]) -> None:
  """Replaces the source registry with stubs.

  Args:
    monkeypatch: pytest's patching fixture.
    answers: Source name to the tags it should return.
  """
  monkeypatch.setattr(main, "SOURCE_TYPES", dict.fromkeys(answers, None))
  monkeypatch.setattr(
      main,
      "build_all",
      lambda names=None:
      [ProbeStub(name, answers[name]) for name in (names or tuple(answers))])


def test_probe_compares_sources_field_by_field(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """The per-track view shows each source's value for each field."""
  fake_sources(
      monkeypatch, {
          "discogs": fields.TrackTags(title="Strobe", remixer="DJ Marky"),
          "spotify": fields.TrackTags(title="Strobe", isrc="GB123"),
      })
  result = runner.invoke(main.app,
                         ["probe", "--artist", "deadmau5", "--title", "Strobe"])
  assert result.exit_code == 0
  assert "DJ Marky" in result.stdout
  assert "GB123" in result.stdout


def test_probe_prints_a_coverage_table(monkeypatch: pytest.MonkeyPatch) -> None:
  """The coverage table is what precedence is actually tuned on."""
  fake_sources(
      monkeypatch, {
          "discogs": fields.TrackTags(title="Strobe"),
          "spotify": fields.TrackTags(title="Strobe", isrc="GB123"),
      })
  result = runner.invoke(main.app, ["probe", "--title", "Strobe"])
  assert "coverage across 1 track(s)" in result.stdout
  assert "isrc" in result.stdout


def test_probe_marks_missing_values(monkeypatch: pytest.MonkeyPatch) -> None:
  """A source with no value for a field shows as absent, not blank."""
  fake_sources(monkeypatch, {
      "discogs": fields.TrackTags(title="Strobe"),
      "spotify": None,
  })
  result = runner.invoke(main.app, ["probe", "--title", "Strobe"])
  assert result.exit_code == 0
  assert "-" in result.stdout


def test_probe_reads_queries_from_files(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """A file's own tags become the search query."""
  audio = conftest.write_m4a(tmp_path / "track.m4a")
  tag_io.write_tags(audio, fields.TrackTags(title="Strobe", artist="deadmau5"))
  fake_sources(monkeypatch, {"spotify": fields.TrackTags(title="Strobe")})
  result = runner.invoke(main.app, ["probe", str(audio)])
  assert result.exit_code == 0
  assert "track.m4a" in result.stdout


def test_probe_uses_the_filename_when_a_file_is_untagged(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """An untagged file is searched for by name rather than skipped.

  This used to skip. It should not: nearly every file here is named
  "Artist - Title", and for WAV that name is the only metadata there is.
  """
  config = build_source_tree(tmp_path, ("Tiesto - All Nighter.m4a",))
  fake_sources(monkeypatch, {"spotify": fields.TrackTags(title="All Nighter")})
  result = runner.invoke(
      main.app,
      ["probe", str(tmp_path / "yt-dlp" / "Tiesto - All Nighter.m4a")])
  assert result.exit_code == 0
  assert "All Nighter" in result.stdout
  del config


def test_probe_needs_something_to_probe(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """With no files and no terms, the command says what it needs."""
  fake_sources(monkeypatch, {"spotify": fields.TrackTags(title="X")})
  result = runner.invoke(main.app, ["probe"])
  assert result.exit_code == 1


def test_probe_rejects_an_unknown_source(
    monkeypatch: pytest.MonkeyPatch) -> None:
  """A misspelled --only names the sources that do exist."""
  fake_sources(monkeypatch, {"spotify": fields.TrackTags(title="X")})
  result = runner.invoke(main.app, ["probe", "--title", "X", "--only", "nope"])
  assert result.exit_code == 1


def fake_matcher(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
  """Replaces the matcher so CLI tests need no network.

  Args:
    monkeypatch: pytest's patching fixture.
    result: The Match to return for every track.
  """
  monkeypatch.setattr(main,
                      "build_all",
                      lambda names=None: [ProbeStub("spotify", None)])
  monkeypatch.setattr(main.match_lib, "match_track",
                      lambda *args, **kwargs: result)


def a_match(status: str = "matched",
            confidence: float = 0.95,
            album: str = "21") -> object:
  """Builds a Match for the CLI to record.

  Args:
    status: The match status.
    confidence: The match confidence.
    album: The album to propose.

  Returns:
    The constructed Match.
  """
  return main.match_lib.Match(tags=fields.TrackTags(title="Hello",
                                                    artist="Adele",
                                                    album=album),
                              confidence=confidence,
                              status=status,
                              candidates={},
                              field_sources={
                                  "title": "spotify",
                                  "album": "spotify"
                              })


def indexed_library(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch) -> tuple[pathlib.Path, pathlib.Path]:
  """Builds a scanned library with one tagged track.

  Args:
    tmp_path: The temporary directory.
    monkeypatch: pytest's patching fixture.

  Returns:
    The sources.toml path and the database path.
  """
  config = build_source_tree(tmp_path, ("a.m4a",))
  tag_io.write_tags(tmp_path / "yt-dlp" / "a.m4a",
                    fields.TrackTags(title="Hello", artist="Adele"))
  fake_fingerprints(monkeypatch, {"a.m4a": BASE_VALUES})
  db_path = tmp_path / "state.db"
  runner.invoke(main.app,
                ["scan", "--sources",
                 str(config), "--db",
                 str(db_path)])
  return config, db_path


def test_match_run_records_a_proposal(tmp_path: pathlib.Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
  """A match is stored against the track without touching the file."""
  config, db_path = indexed_library(tmp_path, monkeypatch)
  before = (tmp_path / "yt-dlp" / "a.m4a").read_bytes()
  fake_matcher(monkeypatch, a_match())

  result = runner.invoke(
      main.app,
      ["match", "run", "--sources",
       str(config), "--db",
       str(db_path)])
  assert result.exit_code == 0
  with connection.open_db(db_path) as conn:
    row = conn.execute("SELECT match_status, match_confidence,"
                       " matched_tags_json FROM tracks").fetchone()
  assert row["match_status"] == "matched"
  assert row["match_confidence"] == pytest.approx(0.95)
  assert "21" in row["matched_tags_json"]
  # The proposal is recorded; writing it is a separate step.
  assert (tmp_path / "yt-dlp" / "a.m4a").read_bytes() == before


def test_match_run_dry_run_writes_nothing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """`--dry-run` reports the work without recording any of it."""
  config, db_path = indexed_library(tmp_path, monkeypatch)
  fake_matcher(monkeypatch, a_match())
  result = runner.invoke(main.app, [
      "match", "run", "--sources",
      str(config), "--db",
      str(db_path), "--dry-run"
  ])
  assert "would match 1 tracks" in result.stdout
  with connection.open_db(db_path) as conn:
    assert conn.execute(
        "SELECT matched_at FROM tracks").fetchone()["matched_at"] is None


def test_match_run_is_resumable(tmp_path: pathlib.Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
  """A second run skips tracks that already have a match."""
  config, db_path = indexed_library(tmp_path, monkeypatch)
  fake_matcher(monkeypatch, a_match())
  args = ["match", "run", "--sources", str(config), "--db", str(db_path)]
  runner.invoke(main.app, args)
  assert "1 already matched, 0 to do" in runner.invoke(main.app, args).stdout


def test_match_summary_counts_statuses(tmp_path: pathlib.Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
  """The summary is how the review queue is sized."""
  config, db_path = indexed_library(tmp_path, monkeypatch)
  fake_matcher(monkeypatch, a_match(status="review", confidence=0.6))
  runner.invoke(
      main.app,
      ["match", "run", "--sources",
       str(config), "--db",
       str(db_path)])
  result = runner.invoke(main.app, ["match", "summary", "--db", str(db_path)])
  assert "review" in result.stdout


def test_match_ignore_stops_a_track_being_matched(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """A self-made edit marked won't-match drops out of the queue."""
  config, db_path = indexed_library(tmp_path, monkeypatch)
  track = tmp_path / "yt-dlp" / "a.m4a"
  ignored = runner.invoke(main.app, [
      "match", "ignore",
      str(track), "--db",
      str(db_path), "--reason", "self-made"
  ])
  assert ignored.exit_code == 0

  fake_matcher(monkeypatch, a_match())
  result = runner.invoke(
      main.app,
      ["match", "run", "--sources",
       str(config), "--db",
       str(db_path)])
  assert "0 tracks" in result.stdout


def test_match_ignore_needs_an_indexed_track(tmp_path: pathlib.Path) -> None:
  """Marking a file that was never scanned says what to do."""
  result = runner.invoke(main.app, [
      "match", "ignore",
      str(tmp_path / "nope.m4a"), "--db",
      str(tmp_path / "state.db")
  ])
  assert result.exit_code == 1


def test_probe_falls_back_to_the_filename(tmp_path: pathlib.Path) -> None:
  """An untagged file is searched for by its name rather than skipped.

  This is the normal case for WAV, whose tagging support is an
  afterthought, and it is how the beatport folder gets matched at all.
  """
  folder = tmp_path / "yt-dlp"
  folder.mkdir(parents=True, exist_ok=True)
  audio = conftest.write_m4a(folder / "Tiesto - All Nighter.m4a")
  query = probe_lib.query_for_file(audio)
  assert query.is_usable()
  assert query.title == "All Nighter"
  assert query.artist == "Tiesto"


def matched_library(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str = "matched") -> tuple[pathlib.Path, pathlib.Path]:
  """Builds a library with one track carrying a recorded match.

  Args:
    tmp_path: The temporary directory.
    monkeypatch: pytest's patching fixture.
    status: The match status to record.

  Returns:
    The audio file and the database path.
  """
  config, db_path = indexed_library(tmp_path, monkeypatch)
  fake_matcher(monkeypatch, a_match(status=status))
  runner.invoke(
      main.app,
      ["match", "run", "--sources",
       str(config), "--db",
       str(db_path)])
  return (tmp_path / "yt-dlp" / "a.m4a", db_path)


def test_apply_writes_matched_tags(tmp_path: pathlib.Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
  """The proposal recorded by `match` is written into the file."""
  audio, db_path = matched_library(tmp_path, monkeypatch)
  result = runner.invoke(main.app, [
      "apply", "--db",
      str(db_path), "--art-store",
      str(tmp_path / "art"), "--skip-art"
  ])
  assert result.exit_code == 0
  assert tag_io.read_tags(audio).album == "21"


def test_apply_dry_run_touches_nothing(tmp_path: pathlib.Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
  """A dry run reports the plan and leaves the file alone."""
  audio, db_path = matched_library(tmp_path, monkeypatch)
  before = audio.read_bytes()
  result = runner.invoke(main.app, ["apply", "--db", str(db_path), "--dry-run"])
  assert "dry run" in result.stdout
  assert audio.read_bytes() == before


def test_apply_skips_matches_queued_for_review(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """A doubtful match must not be written without being asked for."""
  audio, db_path = matched_library(tmp_path, monkeypatch, status="review")
  runner.invoke(main.app, ["apply", "--db", str(db_path), "--skip-art"])
  assert tag_io.read_tags(audio).album != "21"


def test_apply_can_include_review_matches(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """`--include-review` opts into writing the doubtful ones."""
  audio, db_path = matched_library(tmp_path, monkeypatch, status="review")
  runner.invoke(
      main.app,
      ["apply", "--db",
       str(db_path), "--skip-art", "--include-review"])
  assert tag_io.read_tags(audio).album == "21"


def test_undo_lists_the_timeline(tmp_path: pathlib.Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
  """With no options, undo only shows what happened."""
  audio, db_path = matched_library(tmp_path, monkeypatch)
  runner.invoke(main.app, ["apply", "--db", str(db_path), "--skip-art"])
  result = runner.invoke(main.app, ["undo", str(audio), "--db", str(db_path)])
  assert "recorded change" in result.stdout
  assert "album" in result.stdout
  assert tag_io.read_tags(audio).album == "21"


def test_undo_last_restores_the_previous_values(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """The whole point: what was written can be put back."""
  audio, db_path = matched_library(tmp_path, monkeypatch)
  runner.invoke(main.app, ["apply", "--db", str(db_path), "--skip-art"])
  assert tag_io.read_tags(audio).album == "21"
  result = runner.invoke(
      main.app, ["undo", str(audio), "--db",
                 str(db_path), "--last"])
  assert result.exit_code == 0
  assert tag_io.read_tags(audio).album != "21"


def test_undo_dry_run_restores_nothing(tmp_path: pathlib.Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
  """A dry-run undo reports without writing."""
  audio, db_path = matched_library(tmp_path, monkeypatch)
  runner.invoke(main.app, ["apply", "--db", str(db_path), "--skip-art"])
  result = runner.invoke(
      main.app,
      ["undo", str(audio), "--db",
       str(db_path), "--last", "--dry-run"])
  assert "would restore" in result.stdout
  assert tag_io.read_tags(audio).album == "21"


def test_undo_needs_an_indexed_file(tmp_path: pathlib.Path) -> None:
  """A file that was never scanned has no history to show."""
  result = runner.invoke(
      main.app,
      ["undo",
       str(tmp_path / "nope.m4a"), "--db",
       str(tmp_path / "state.db")])
  assert result.exit_code == 1


def test_undo_rejects_an_unknown_batch(tmp_path: pathlib.Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
  """A mistyped batch id is refused rather than silently doing nothing."""
  audio, db_path = matched_library(tmp_path, monkeypatch)
  runner.invoke(main.app, ["apply", "--db", str(db_path), "--skip-art"])
  result = runner.invoke(
      main.app,
      ["undo", str(audio), "--db",
       str(db_path), "--to", "not-a-batch"])
  assert result.exit_code == 1


def test_undo_accepts_the_shortened_batch_id(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
  """The id the timeline prints has to be the id `--to` accepts.

  The timeline shortens a 32-character identifier for readability, so
  requiring the full one back would make the displayed value useless.
  """
  audio, db_path = matched_library(tmp_path, monkeypatch)
  runner.invoke(main.app, ["apply", "--db", str(db_path), "--skip-art"])
  listing = runner.invoke(
      main.app,
      ["undo", str(audio), "--db", str(db_path)]).stdout
  shown = [
      line.strip().split()[0]
      for line in listing.splitlines()
      if line.startswith("  ") and len(line.strip().split()[0]) == 12
  ]
  assert shown, "no batch id was printed"
  result = runner.invoke(
      main.app,
      ["undo", str(audio), "--db",
       str(db_path), "--to", shown[0]])
  assert result.exit_code == 0
  assert tag_io.read_tags(audio).album != "21"


def test_undo_rejects_an_ambiguous_prefix() -> None:
  """A prefix matching two writes is refused rather than guessed at."""
  with pytest.raises(LookupError, match="matches 2 batches"):
    main.resolve_batch("ab", ["abcd", "abef"])
