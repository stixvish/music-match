"""The `music-match` command-line entry point.

Commands land with the pipeline stage they belong to. So far: inspecting
resolved config, creating the database, dumping a file's tags,
fingerprinting the library, and the duplicate scan.
"""

import pathlib
import shutil
import sqlite3
from typing import Annotated, Iterable

import typer

from music_match import __version__
from music_match import library
from music_match.config import loader
from music_match.db import connection
from music_match.db import queries
from music_match.db import schema
from music_match.tagging import dedup as dedup_lib
from music_match.tagging import fingerprint as fp
from music_match.tagging import quality as quality_lib
from music_match.tagging import tags as tag_io

app = typer.Typer(
    help="Tag a personal music library from public metadata sources.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Inspect resolved configuration.",
                         no_args_is_help=True)
db_app = typer.Typer(help="Manage local SQLite state.", no_args_is_help=True)
tags_app = typer.Typer(help="Read file tags.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(tags_app, name="tags")

SourcesOption = Annotated[
    pathlib.Path,
    typer.Option("--sources", help="Path to sources.toml.")]
PrecedenceOption = Annotated[
    pathlib.Path,
    typer.Option("--precedence", help="Path to precedence.toml.")]
DbOption = Annotated[pathlib.Path,
                     typer.Option("--db", help="Path to the SQLite database.")]
SourceNameOption = Annotated[
    str | None,
    typer.Option("--source", help="Limit to one configured source folder.")]
DryRunOption = Annotated[bool,
                         typer.Option("--dry-run",
                                      help="Report what would change without "
                                      "writing anything.")]


def _joined(names: Iterable[str]) -> str:
  """Joins names for display in a single line of output.

  Args:
    names: The strings to join.

  Returns:
    The names separated by commas.
  """
  return ", ".join(names)


@app.command()
def version() -> None:
  """Prints the installed version."""
  typer.echo(__version__)


@config_app.command("show")
def config_show(
    sources: SourcesOption = loader.DEFAULT_SOURCES_FILE,
    precedence: PrecedenceOption = loader.DEFAULT_PRECEDENCE_FILE,
) -> None:
  """Prints the configured source folders and genre source precedence.

  Args:
    sources: Path to sources.toml.
    precedence: Path to precedence.toml.
  """
  sources_config = _load_sources_or_exit(sources)
  precedence_config = _load_precedence_or_exit(precedence)

  typer.echo("source folders:")
  for folder in sources_config.folders.values():
    exists = "ok" if folder.path.is_dir() else "MISSING"
    typer.echo(f"  {folder.name}: {folder.path} [{exists}]"
               f" video-rip check: {folder.check_for_video_rips}")

  typer.echo("\nsource precedence:")
  for genre, entry in precedence_config.genres.items():
    typer.echo(f"  {genre}: {_joined(entry.order)}")
    for field, order in entry.fields.items():
      typer.echo(f"    {field}: {_joined(order)}")


@db_app.command("init")
def db_init(
    db: DbOption = connection.DEFAULT_DB_FILE,
    dry_run: DryRunOption = False,
) -> None:
  """Creates the database and any missing tables.

  Safe to re-run: existing tables and data are left alone.

  Args:
    db: Path to the SQLite database file.
    dry_run: Report what would be created without touching disk.
  """
  if dry_run:
    existed = db.exists()
    state = "already exists" if existed else "would be created"
    typer.echo(f"dry run: {db} {state}")
    typer.echo(f"dry run: would ensure tables {_joined(schema.TABLES)}")
    return

  created = not db.exists()
  with connection.open_db(db) as conn:
    present = connection.table_names(conn)
    version_number = connection.schema_version(conn)
  action = "created" if created else "updated"
  typer.echo(f"{action} {db} (schema v{version_number})")
  typer.echo(f"tables: {_joined(present)}")


@tags_app.command("show")
def tags_show(
    file: Annotated[pathlib.Path,
                    typer.Argument(help="Audio file to read.")],
    show_empty: Annotated[
        bool,
        typer.Option("--show-empty", help="Include fields that are "
                     "unset.")] = False,
) -> None:
  """Prints a file's tags as this tool sees them.

  Args:
    file: The audio file to read.
    show_empty: Whether to list fields the file does not set.
  """
  try:
    track = tag_io.read_tags(file)
  except tag_io.TagError as err:
    typer.echo(f"error: {err}", err=True)
    raise typer.Exit(code=1) from err

  values = track.as_dict(include_empty=show_empty)
  if not values:
    typer.echo("no tags found")
    return
  width = max(len(name) for name in values)
  for name, value in values.items():
    text = "" if value is None else value
    typer.echo(f"{name.ljust(width)}  {text}")


def _load_sources_or_exit(path: pathlib.Path) -> loader.SourcesConfig:
  """Loads sources.toml, exiting with a clear message on failure.

  Args:
    path: Path to sources.toml.

  Returns:
    The parsed configuration.

  Raises:
    typer.Exit: If the config is missing or invalid.
  """
  try:
    return loader.load_sources(path)
  except loader.ConfigError as err:
    typer.echo(f"error: {err}", err=True)
    raise typer.Exit(code=1) from err


def _load_precedence_or_exit(path: pathlib.Path) -> loader.PrecedenceConfig:
  """Loads precedence.toml, exiting with a clear message on failure.

  Args:
    path: Path to precedence.toml.

  Returns:
    The parsed configuration.

  Raises:
    typer.Exit: If the config is missing or invalid.
  """
  try:
    return loader.load_precedence(path)
  except loader.ConfigError as err:
    typer.echo(f"error: {err}", err=True)
    raise typer.Exit(code=1) from err


@app.command("scan")
def scan(
    sources: SourcesOption = loader.DEFAULT_SOURCES_FILE,
    db: DbOption = connection.DEFAULT_DB_FILE,
    source: SourceNameOption = None,
    limit: Annotated[
        int,
        typer.Option("--limit",
                     help="Stop after this many files. 0 means no limit.")] = 0,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-fingerprint files already indexed."
                    )] = False,
    dry_run: DryRunOption = False,
) -> None:
  """Fingerprints the library and records the results.

  Resumable: files already fingerprinted are skipped unless --force is
  given, so a run that dies partway is not wasted work.

  Args:
    sources: Path to sources.toml.
    db: Path to the SQLite database.
    source: Limit to one configured source folder.
    limit: Stop after this many files, or 0 for no limit.
    force: Re-fingerprint files that already have a fingerprint.
    dry_run: Report what would be fingerprinted without writing.
  """
  sources_config = _load_sources_or_exit(sources)
  if not fp.have_fpcalc():
    typer.echo(
        "error: fpcalc not found on PATH. Install chromaprint (see SETUP.md).",
        err=True)
    raise typer.Exit(code=1)

  try:
    files = list(library.walk(sources_config, source))
  except KeyError:
    typer.echo(f"error: no source folder named '{source}' in {sources}",
               err=True)
    raise typer.Exit(code=1) from None
  except FileNotFoundError as err:
    typer.echo(f"error: {err}", err=True)
    raise typer.Exit(code=1) from err

  with connection.open_db(db) as conn:
    done = set() if force else queries.fingerprinted_paths(conn)
    pending = [item for item in files if str(item.path) not in done]
    # Counted before --limit truncates, so a limited run does not report
    # the files it is deferring as ones it had already done.
    already = len(files) - len(pending)
    if limit:
      pending = pending[:limit]

    typer.echo(f"{len(files)} audio files, {already} already fingerprinted, "
               f"{len(pending)} to do")
    if dry_run:
      typer.echo(f"dry run: would fingerprint {len(pending)} files")
      return

    failures = _fingerprint_all(conn, pending)

  typer.echo(f"fingerprinted {len(pending) - failures}, failed {failures}")


def _fingerprint_all(conn: sqlite3.Connection,
                     pending: list[library.LibraryFile]) -> int:
  """Fingerprints files and records each result as it completes.

  Committing per file is what makes a long run resumable — an
  interrupted scan keeps everything it had already done.

  Args:
    conn: An open database connection.
    pending: The files to fingerprint.

  Returns:
    How many files failed.
  """
  failures = 0
  for position, item in enumerate(pending, start=1):
    report = position % 50 == 0 or position == len(pending)
    try:
      fingerprint = fp.fingerprint_file(item.path)
      queries.upsert_track(conn,
                           path=item.path,
                           source_name=item.source.name,
                           fingerprint=fingerprint.encode(),
                           duration_seconds=fingerprint.duration)
      conn.commit()
      if report:
        typer.echo(f"  {position}/{len(pending)}")
    except fp.FingerprintError as err:
      failures += 1
      typer.echo(f"  skipped: {err}", err=True)
  return failures


@app.command("dedup")
def dedup(
    sources: SourcesOption = loader.DEFAULT_SOURCES_FILE,
    db: DbOption = connection.DEFAULT_DB_FILE,
    source: SourceNameOption = None,
    threshold: Annotated[
        float,
        typer.Option("--threshold",
                     help="Similarity score at which two tracks are the "
                     "same recording.")] = dedup_lib.
    DEFAULT_SIMILARITY_THRESHOLD,
    apply_moves: Annotated[
        bool,
        typer.Option("--apply",
                     help="Actually move the duplicates. Without this, the "
                     "scan only reports.")] = False,
) -> None:
  """Finds duplicate recordings by fingerprint and moves the losers.

  Reports without touching anything unless --apply is given. The
  higher-quality copy is kept: lossless beats lossy, then bitrate. The
  loser is moved to the folder configured under [duplicates], never
  deleted.

  Args:
    sources: Path to sources.toml.
    db: Path to the SQLite database.
    source: Limit to one configured source folder.
    threshold: Similarity score at which two tracks are the same.
    apply_moves: Perform the moves rather than only reporting them.
  """
  sources_config = _load_sources_or_exit(sources)
  with connection.open_db(db) as conn:
    tracks = _load_indexed_tracks(conn, source)
    if not tracks:
      typer.echo("no fingerprinted tracks; run `music-match scan` first")
      return

    typer.echo(f"comparing {len(tracks)} fingerprinted tracks")
    groups = dedup_lib.find_duplicates(tracks, threshold=threshold)
    if not groups:
      typer.echo("no duplicates found")
      return

    total = sum(len(group.duplicates) for group in groups)
    typer.echo(f"{len(groups)} duplicated recordings, {total} extra copies\n")
    for group in groups:
      _report_group(group)

    if not apply_moves:
      typer.echo("reported only. Re-run with --apply to move the duplicates.")
      return
    moved = _apply_group_moves(conn, groups, sources_config)
    typer.echo(f"moved {moved} duplicates to {sources_config.duplicates_path}")


def _report_group(group: dedup_lib.DuplicateGroup) -> None:
  """Prints one duplicate group.

  Args:
    group: The group to describe.
  """
  typer.echo(f"keep  {group.keeper.path.name}")
  typer.echo(f"      {group.keeper.quality.describe()}")
  for duplicate in group.duplicates:
    typer.echo(f"  dup {duplicate.track.path.name}")
    typer.echo(f"      {duplicate.track.quality.describe()}"
               f"  similarity {duplicate.similarity:.3f}")
  typer.echo("")


def _load_indexed_tracks(conn: sqlite3.Connection,
                         source: str | None) -> list[dedup_lib.IndexedTrack]:
  """Loads fingerprinted tracks whose files are still present.

  Args:
    conn: An open database connection.
    source: Limit to one source folder, or None for all.

  Returns:
    The tracks dedup can compare.
  """
  tracks = []
  for row in queries.fingerprinted_tracks(conn, source):
    path = pathlib.Path(row["path"])
    if not path.is_file():
      continue
    try:
      fingerprint = fp.decode(row["fingerprint"], row["duration_seconds"] or
                              0.0)
      audio_quality = quality_lib.probe(path)
    except (fp.FingerprintError, quality_lib.QualityError) as err:
      typer.echo(f"  skipped: {err}", err=True)
      continue
    tracks.append(
        dedup_lib.IndexedTrack(track_id=int(row["id"]),
                               path=path,
                               fingerprint=fingerprint,
                               quality=audio_quality))
  return tracks


def _apply_group_moves(conn: sqlite3.Connection,
                       groups: list[dedup_lib.DuplicateGroup],
                       sources_config: loader.SourcesConfig) -> int:
  """Moves every duplicate out of the library.

  Args:
    conn: An open database connection.
    groups: The duplicate groups to resolve.
    sources_config: The loaded source configuration.

  Returns:
    How many files were moved.
  """
  moved = 0
  for group in groups:
    for duplicate in group.duplicates:
      folder = sources_config.folder_for_path(duplicate.track.path)
      if folder is None:
        typer.echo(
            f"  refusing to move {duplicate.track.path}:"
            " not under a configured source folder",
            err=True)
        continue
      try:
        destination = dedup_lib.destination_for(duplicate.track, folder.name,
                                                sources_config.duplicates_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(duplicate.track.path), str(destination))
      except (OSError, FileExistsError) as err:
        # One unmovable file must not abandon the rest of the run.
        typer.echo(f"  could not move {duplicate.track.path}: {err}", err=True)
        continue
      queries.delete_track(conn, duplicate.track.track_id)
      conn.commit()
      moved += 1
  return moved
