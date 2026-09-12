"""`music reset` (SPEC.md §13)."""

import pytest

from music import cli, config, db


@pytest.fixture
def populated(isolated_paths, monkeypatch):
  """A library, staging dir and database with something in all three."""
  cfg = config.load()
  cfg.paths.library.mkdir(parents=True, exist_ok=True)
  cfg.paths.staging.mkdir(parents=True, exist_ok=True)
  (cfg.paths.library / "a.aiff").write_bytes(b"x" * 100)
  (cfg.paths.staging / "b.m4a").write_bytes(b"y" * 100)
  conn = db.connect(cfg.paths.database)
  db.migrate(conn)
  conn.execute(
    "INSERT INTO source_file (id, origin, staging_path, sha256, duration_s)"
    " VALUES (1,'youtube','s','h',1.0)"
  )
  conn.execute("INSERT INTO track (id, source_file_id) VALUES (1,1)")
  conn.close()
  return cfg


def run(argv):
  args = cli.build_parser().parse_args(argv)
  return args.func(args)


def test_reset_removes_everything(populated):
  assert run(["reset", "--yes"]) == 0
  assert not populated.paths.library.exists()
  assert not populated.paths.staging.exists()
  assert not populated.paths.database.exists()


def test_reset_without_confirmation_deletes_nothing(populated, monkeypatch):
  """The typed confirmation is the only thing standing in front of the library."""
  monkeypatch.setattr("sys.stdin", __import__("io").StringIO("yes\n"))
  assert run(["reset"]) == 1
  assert populated.paths.library.exists()
  assert populated.paths.database.exists()


def test_the_right_phrase_confirms(populated, monkeypatch):
  monkeypatch.setattr("sys.stdin", __import__("io").StringIO("delete 3\n"))
  assert run(["reset"]) == 0
  assert not populated.paths.library.exists()


def test_keep_staging_leaves_the_downloads(populated):
  assert run(["reset", "--yes", "--keep-staging"]) == 0
  assert not populated.paths.library.exists()
  assert not populated.paths.database.exists()
  assert (populated.paths.staging / "b.m4a").exists()


def test_reset_on_an_empty_setup_is_a_no_op(isolated_paths):
  assert run(["reset"]) == 0


def test_wal_sidecars_are_removed(populated):
  """A stale -wal beside a fresh database reads as corruption."""
  wal = populated.paths.database.with_name(populated.paths.database.name + "-wal")
  wal.write_bytes(b"stale")
  assert run(["reset", "--yes"]) == 0
  assert not wal.exists()
