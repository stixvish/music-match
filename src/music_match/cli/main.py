"""The `music-match` command-line entry point.

Commands land with the pipeline stage they belong to. So far: inspecting
resolved config, creating the database, dumping a file's tags,
fingerprinting the library, and the duplicate scan.
"""

import json
import pathlib
import shutil
import sqlite3
from typing import Annotated, Iterable

import requests
import typer

from music_match import __version__
from music_match import library
from music_match import intake as intake_lib
from music_match import probe as probe_lib
from music_match.matching import matcher as match_lib
from music_match.config import env
from music_match.config import loader
from music_match.db import connection
from music_match.db import queries
from music_match.db import schema
from music_match.tagging import apply as apply_lib
from music_match.tagging import art as art_lib
from music_match.tagging import dedup as dedup_lib
from music_match.tagging import fields as tag_fields
from music_match.tagging import fingerprint as fp
from music_match.tagging import genre as genre_lib
from music_match.tagging import quality as quality_lib
from music_match.sources import SOURCE_TYPES
from music_match.sources import base as source_base
from music_match.sources import build_all
from music_match.tagging import tags as tag_io
from music_match.tagging import videorip as videorip_lib

app = typer.Typer(
    help="Tag a personal music library from public metadata sources.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Inspect resolved configuration.",
                         no_args_is_help=True)
db_app = typer.Typer(help="Manage local SQLite state.", no_args_is_help=True)
tags_app = typer.Typer(help="Read file tags.", no_args_is_help=True)
genre_app = typer.Typer(help="Local genre detection.", no_args_is_help=True)
rips_app = typer.Typer(help="Find and quarantine video rips.",
                       no_args_is_help=True)
match_app = typer.Typer(help="Match tracks against metadata sources.",
                        no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(tags_app, name="tags")
app.add_typer(genre_app, name="genre")
app.add_typer(match_app, name="match")
app.add_typer(rips_app, name="video-rips")

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


ModelsOption = Annotated[
    pathlib.Path,
    typer.Option("--models", help="Directory holding the Essentia models.")]


@genre_app.command("fetch-models")
def genre_fetch_models(
    models: ModelsOption = genre_lib.DEFAULT_MODELS_DIR,
    dry_run: DryRunOption = False,
) -> None:
  """Downloads the discogs-effnet model files.

  About 20MB in total, and gitignored. Files already present are left
  alone, so this is safe to re-run.

  Args:
    models: Where to put the model files.
    dry_run: Report what would be downloaded without writing.
  """
  missing = genre_lib.missing_models(models)
  if not missing:
    typer.echo(f"all model files already present in {models}")
    return
  if dry_run:
    typer.echo(f"dry run: would download {len(missing)} files to {models}")
    for name in missing:
      typer.echo(f"  {name}")
    return

  models.mkdir(parents=True, exist_ok=True)
  for name in missing:
    typer.echo(f"downloading {name}")
    try:
      _download(genre_lib.MODEL_URLS[name], models / name)
    except requests.RequestException as err:
      typer.echo(f"error: could not download {name}: {err}", err=True)
      raise typer.Exit(code=1) from err
  typer.echo(f"models ready in {models}")


def _download(url: str, destination: pathlib.Path) -> None:
  """Streams a file to disk, writing to a temporary name first.

  Writing to a `.part` file and renaming on success means an interrupted
  download never leaves something that looks like a usable model.

  Args:
    url: What to download.
    destination: Where it should end up.

  Raises:
    requests.RequestException: If the download fails.
  """
  partial = destination.with_suffix(destination.suffix + ".part")
  with requests.get(url, stream=True, timeout=60) as response:
    response.raise_for_status()
    with partial.open("wb") as handle:
      for chunk in response.iter_content(chunk_size=1 << 16):
        handle.write(chunk)
  partial.replace(destination)


@genre_app.command("show")
def genre_show(
    file: Annotated[pathlib.Path,
                    typer.Argument(help="Audio file to analyse.")],
    models: ModelsOption = genre_lib.DEFAULT_MODELS_DIR,
    top: Annotated[int,
                   typer.Option("--top", help="How many predictions to show."
                               )] = genre_lib.DEFAULT_TOP_N,
) -> None:
  """Prints what the model makes of one file.

  Args:
    file: The audio file to analyse.
    models: Directory holding the Essentia models.
    top: How many predictions to show.
  """
  detector = _detector_or_exit(models, top)
  try:
    result = detector.detect(file)
  except genre_lib.GenreError as err:
    typer.echo(f"error: {err}", err=True)
    raise typer.Exit(code=1) from err

  if not result.predictions:
    typer.echo("no prediction: too little audio to analyse")
    return
  for prediction in result.predictions:
    typer.echo(f"  {prediction.confidence:.3f}  {prediction.label}")
  key = loader.normalize_genre(result.label or "")
  typer.echo(f"\nprecedence key: {key}")


@genre_app.command("index")
def genre_index(
    sources: SourcesOption = loader.DEFAULT_SOURCES_FILE,
    db: DbOption = connection.DEFAULT_DB_FILE,
    models: ModelsOption = genre_lib.DEFAULT_MODELS_DIR,
    source: SourceNameOption = None,
    limit: Annotated[
        int,
        typer.Option("--limit",
                     help="Stop after this many files. 0 means no limit.")] = 0,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-detect files already done.")] = False,
    dry_run: DryRunOption = False,
) -> None:
  """Detects the genre of every file and records it.

  Resumable in the same way as `scan`: files already carrying a detected
  genre are skipped unless --force is given.

  Args:
    sources: Path to sources.toml.
    db: Path to the SQLite database.
    models: Directory holding the Essentia models.
    source: Limit to one configured source folder.
    limit: Stop after this many files, or 0 for no limit.
    force: Re-detect files that already have a genre.
    dry_run: Report what would be analysed without writing.
  """
  sources_config = _load_sources_or_exit(sources)
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
    done = set() if force else queries.genre_tagged_paths(conn)
    pending = [item for item in files if str(item.path) not in done]
    already = len(files) - len(pending)
    if limit:
      pending = pending[:limit]
    typer.echo(f"{len(files)} audio files, {already} already analysed, "
               f"{len(pending)} to do")
    if dry_run:
      typer.echo(f"dry run: would analyse {len(pending)} files")
      return
    if not pending:
      return

    detector = _detector_or_exit(models, genre_lib.DEFAULT_TOP_N)
    failures = _detect_all(conn, detector, pending)

  typer.echo(f"analysed {len(pending) - failures}, failed {failures}")


def _detect_all(conn: sqlite3.Connection, detector: genre_lib.GenreDetector,
                pending: list[library.LibraryFile]) -> int:
  """Detects genres and records each result as it completes.

  Args:
    conn: An open database connection.
    detector: The loaded detector.
    pending: The files to analyse.

  Returns:
    How many files failed.
  """
  failures = 0
  for position, item in enumerate(pending, start=1):
    report = position % 25 == 0 or position == len(pending)
    try:
      result = detector.detect(item.path)
    except genre_lib.GenreError as err:
      failures += 1
      typer.echo(f"  skipped: {err}", err=True)
      continue
    top = result.top
    if top is None:
      failures += 1
      typer.echo(f"  skipped: no prediction for {item.path}", err=True)
      continue
    queries.upsert_track(conn,
                         path=item.path,
                         source_name=item.source.name,
                         detected_genre=top.label,
                         genre_confidence=top.confidence)
    conn.commit()
    if report:
      typer.echo(f"  {position}/{len(pending)}")
  return failures


@genre_app.command("summary")
def genre_summary(
    db: DbOption = connection.DEFAULT_DB_FILE,
    top: Annotated[int,
                   typer.Option("--top", help="How many genres to list.")] = 20,
    min_confidence: Annotated[
        float,
        typer.Option("--min-confidence",
                     help="Ignore labels the model backed less strongly "
                     "than this.")] = 0.0,
) -> None:
  """Summarises the detected genres already recorded.

  Args:
    db: Path to the SQLite database.
    top: How many genres to list.
    min_confidence: Ignore labels backed less strongly than this.
  """
  with connection.open_db(db) as conn:
    counts = queries.detected_genre_counts(conn, min_confidence)
  if not counts:
    typer.echo("no genres detected yet; run `music-match genre index` first")
    return
  total = sum(count for _, count, _ in counts)
  typer.echo(f"{total} tracks across {len(counts)} labels")
  weak = sum(count for _, count, mean in counts
             if mean < genre_lib.DEFAULT_CONFIDENCE_FLOOR)
  if weak and not min_confidence:
    typer.echo(f"{weak} of them average below "
               f"{genre_lib.DEFAULT_CONFIDENCE_FLOOR} confidence, where the"
               " model is mostly guessing")
  typer.echo("")
  for label, count, mean in counts[:top]:
    typer.echo(f"  {count:5d}  {mean:.2f}  {label}")


def _detector_or_exit(models: pathlib.Path,
                      top_n: int) -> genre_lib.GenreDetector:
  """Builds a detector, exiting with a clear message if it cannot.

  Args:
    models: Directory holding the Essentia models.
    top_n: How many predictions to keep.

  Returns:
    The loaded detector.

  Raises:
    typer.Exit: If Essentia or the model files are missing.
  """
  try:
    return genre_lib.GenreDetector(models, top_n=top_n)
  except genre_lib.GenreError as err:
    typer.echo(f"error: {err}", err=True)
    raise typer.Exit(code=1) from err


@app.command("probe")
def probe(
    files: Annotated[list[pathlib.Path] | None,
                     typer.Argument(
                         help="Audio files to probe. Their tags become the "
                         "search query.")] = None,
    artist: Annotated[
        str | None,
        typer.Option("--artist", help="Probe this artist instead of a "
                     "file.")] = None,
    title: Annotated[
        str | None,
        typer.Option("--title", help="Probe this title instead of a "
                     "file.")] = None,
    only: Annotated[str | None,
                    typer.Option("--only",
                                 help="Comma-separated sources to ask, "
                                 "instead of all of them.")] = None,
    show_all: Annotated[
        bool,
        typer.Option("--all-fields", help="Include fields no source answered."
                    )] = False,
    no_cache: Annotated[
        bool,
        typer.Option("--no-cache", help="Ignore cached responses and re-query."
                    )] = False,
) -> None:
  """Compares what every metadata source says about the same tracks.

  This is how precedence.toml gets tuned: run a sample of real tracks
  through every source and read the per-field comparison and the coverage
  summary, rather than guessing which source is best at what.

  Args:
    files: Audio files whose tags become the search queries.
    artist: Probe this artist rather than a file.
    title: Probe this title rather than a file.
    only: Restrict to these sources.
    show_all: Include fields no source answered.
    no_cache: Ignore cached responses.
  """
  env.load_env()
  names = _requested_sources(only)
  probe_sources = build_all(names)
  if no_cache:
    _disable_caches(probe_sources)

  pending = _probe_queries(files, artist, title)
  if not pending:
    typer.echo("nothing to probe: give audio files, or --artist/--title",
               err=True)
    raise typer.Exit(code=1)

  probes = []
  for path, query in pending:
    typer.echo(f"probing {path.name if path else query.as_text()} ...")
    probes.append(probe_lib.probe_query(query, probe_sources, path))
  report = probe_lib.ProbeReport(probes=tuple(probes), source_names=names)

  for entry in report.probes:
    _render_probe(entry, names, show_all)
  _render_coverage(report)


def _requested_sources(only: str | None) -> tuple[str, ...]:
  """Resolves which sources to ask.

  Args:
    only: Comma-separated names, or None for all registered sources.

  Returns:
    The source names, in registry order.

  Raises:
    typer.Exit: If a named source is not registered.
  """
  if not only:
    return tuple(SOURCE_TYPES)
  wanted = tuple(name.strip() for name in only.split(",") if name.strip())
  unknown = [name for name in wanted if name not in SOURCE_TYPES]
  if unknown:
    typer.echo(
        f"error: unknown source(s): {_joined(unknown)}."
        f" Known: {_joined(SOURCE_TYPES)}",
        err=True)
    raise typer.Exit(code=1)
  return wanted


def _disable_caches(probe_sources: list[source_base.MetadataSource]) -> None:
  """Turns off response caching for a set of sources.

  Args:
    probe_sources: The sources to modify in place.
  """
  for source in probe_sources:
    source.disable_cache()


def _probe_queries(
    files: list[pathlib.Path] | None, artist: str | None, title: str | None
) -> list[tuple[pathlib.Path | None, source_base.SourceQuery]]:
  """Builds the queries to probe, from files or from explicit terms.

  Args:
    files: Audio files whose tags become queries.
    artist: An explicit artist.
    title: An explicit title.

  Returns:
    (path, query) pairs. The path is None for explicit terms.
  """
  probe_queries: list[tuple[pathlib.Path | None, source_base.SourceQuery]] = []
  if title or artist:
    probe_queries.append(
        (None, source_base.SourceQuery(title=title, artist=artist)))
  for path in files or []:
    try:
      query = probe_lib.query_for_file(path)
    except tag_io.TagError as err:
      typer.echo(f"  skipped {path}: {err}", err=True)
      continue
    if not query.is_usable():
      typer.echo(f"  skipped {path}: no title tag to search on", err=True)
      continue
    probe_queries.append((path, query))
  return probe_queries


def _render_probe(entry: probe_lib.TrackProbe, names: tuple[str, ...],
                  show_all: bool) -> None:
  """Prints one track's per-field comparison.

  Args:
    entry: The probed track.
    names: The sources asked, in display order.
    show_all: Whether to include fields no source answered.
  """
  typer.echo(f"\n=== {entry.label()} ===")
  if entry.query.artist or entry.query.title:
    typer.echo(f"    query: {entry.query.as_text()}")
  for source, message in entry.errors.items():
    typer.echo(f"    {source}: {message}")

  width = max(len(name) for name in names)
  for field in probe_lib.COMPARED_FIELDS:
    values = {source: entry.value(source, field) for source in names}
    if not show_all and not any(values.values()):
      continue
    typer.echo(f"  {field}")
    for source in names:
      shown = values[source] if values[source] is not None else "-"
      typer.echo(f"    {source.ljust(width)}  {shown}")

  extras = {
      source: dict(result.extra)
      for source, result in entry.results.items()
      if result is not None and result.extra
  }
  for source, extra in extras.items():
    detail = "  ".join(f"{key}={value}" for key, value in extra.items())
    typer.echo(f"  extra ({source}): {detail}")


def _render_coverage(report: probe_lib.ProbeReport) -> None:
  """Prints the aggregate coverage table.

  Args:
    report: The finished probe report.
  """
  total = len(report.probes)
  names = report.source_names
  typer.echo(f"\n=== coverage across {total} track(s) ===")
  header = "  ".join(name.center(11) for name in names)
  field_column = "field".ljust(16)
  typer.echo(f"  {field_column}{header}")
  coverage = report.coverage()
  for field in report.populated_fields():
    cells = "  ".join(
        f"{coverage[field][name]}/{total}".center(11) for name in names)
    typer.echo(f"  {field.ljust(16)}{cells}")

  art = report.art_coverage()
  art_cells = "  ".join(f"{art[name]}/{total}".center(11) for name in names)
  art_label = "album art".ljust(16)
  typer.echo(f"  {art_label}{art_cells}")

  failures = report.failures()
  if failures:
    typer.echo("")
    for source, count in failures.items():
      typer.echo(f"  {source} failed on {count}/{total}")


AutoApplyOption = Annotated[
    float,
    typer.Option("--auto-apply",
                 help="Confidence at or above which a match is trusted.")]
ReviewFloorOption = Annotated[
    float,
    typer.Option("--review-floor",
                 help="Confidence below which a match is discarded rather "
                 "than queued for review.")]


@match_app.command("run")
def match_run(
    sources: SourcesOption = loader.DEFAULT_SOURCES_FILE,
    precedence: PrecedenceOption = loader.DEFAULT_PRECEDENCE_FILE,
    db: DbOption = connection.DEFAULT_DB_FILE,
    source: SourceNameOption = None,
    limit: Annotated[
        int,
        typer.
        Option("--limit", help="Stop after this many tracks. 0 means no limit."
              )] = 0,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-match tracks already done.")] = False,
    auto_apply: AutoApplyOption = match_lib.DEFAULT_AUTO_APPLY,
    review_floor: ReviewFloorOption = match_lib.DEFAULT_REVIEW_FLOOR,
    dry_run: DryRunOption = False,
) -> None:
  """Matches indexed tracks against the metadata sources.

  Records the proposal only. Nothing is written to any audio file: tag
  writing is a separate step, so a low-confidence match can sit in the
  review queue without touching anything.

  Args:
    sources: Path to sources.toml.
    precedence: Path to precedence.toml.
    db: Path to the SQLite database.
    source: Limit to one configured source folder.
    limit: Stop after this many tracks, or 0 for no limit.
    force: Re-match tracks that already have a match.
    auto_apply: Confidence at or above which a match is trusted.
    review_floor: Confidence below which a match is discarded.
    dry_run: Report what would be matched without writing.
  """
  env.load_env()
  _load_sources_or_exit(sources)
  precedence_config = _load_precedence_or_exit(precedence)
  available = [item for item in build_all() if item.is_available()]
  if not available:
    typer.echo("error: no metadata source has credentials configured", err=True)
    raise typer.Exit(code=1)
  typer.echo(f"sources: {_joined(item.name for item in available)}")

  with connection.open_db(db) as conn:
    rows = list(queries.tracks_for_matching(conn, source))
    pending = [row for row in rows if force or row["matched_at"] is None]
    already = len(rows) - len(pending)
    if limit:
      pending = pending[:limit]
    typer.echo(f"{len(rows)} tracks, {already} already matched, "
               f"{len(pending)} to do")
    if dry_run:
      typer.echo(f"dry run: would match {len(pending)} tracks")
      return
    counts = _match_all(conn, pending, available, precedence_config, auto_apply,
                        review_floor)

  for status, count in sorted(counts.items()):
    typer.echo(f"  {status}: {count}")


def _match_all(conn: sqlite3.Connection, pending: list[sqlite3.Row],
               available: list[source_base.MetadataSource],
               precedence_config: loader.PrecedenceConfig, auto_apply: float,
               review_floor: float) -> dict[str, int]:
  """Matches each track and records the result as it completes.

  Args:
    conn: An open database connection.
    pending: The track rows to match.
    available: The sources with credentials.
    precedence_config: The loaded precedence configuration.
    auto_apply: Confidence at or above which a match is trusted.
    review_floor: Confidence below which a match is discarded.

  Returns:
    Counts per resulting status.
  """
  counts: dict[str, int] = {}
  for position, row in enumerate(pending, start=1):
    path = pathlib.Path(row["path"])
    try:
      query = probe_lib.query_for_file(path, row["duration_seconds"])
    except tag_io.TagError as err:
      typer.echo(f"  skipped {path.name}: {err}", err=True)
      continue
    if not query.is_usable():
      typer.echo(f"  skipped {path.name}: no title to search on", err=True)
      continue

    try:
      result = match_lib.match_track(query,
                                     available,
                                     precedence_config,
                                     genre=row["detected_genre"],
                                     auto_apply=auto_apply,
                                     review_floor=review_floor)
    except Exception as err:  # pylint: disable=broad-exception-caught
      # A long batch must survive one bad track, but the failure is
      # reported loudly rather than being recorded as "no match" — that
      # would look like a verdict instead of a crash.
      typer.echo(f"  ERROR on {path.name}: {type(err).__name__}: {err}",
                 err=True)
      continue
    queries.record_match(
        conn,
        track_id=int(row["id"]),
        source=_primary_source(result),
        confidence=result.confidence,
        status=result.status,
        tags_json=json.dumps(result.tags.as_dict()) if result.tags else None,
        art_url=result.art_url)
    conn.commit()
    counts[result.status] = counts.get(result.status, 0) + 1
    if position % 10 == 0 or position == len(pending):
      typer.echo(f"  {position}/{len(pending)}")
  return counts


def _primary_source(result: match_lib.Match) -> str | None:
  """Returns the source that supplied the most fields of a match.

  Args:
    result: The finished match.

  Returns:
    The source name, or None if nothing matched.
  """
  if not result.field_sources:
    return None
  counts: dict[str, int] = {}
  for name in result.field_sources.values():
    counts[name] = counts.get(name, 0) + 1
  return max(counts, key=lambda name: counts[name])


@match_app.command("show")
def match_show(
    file: Annotated[pathlib.Path,
                    typer.Argument(help="Audio file to match.")],
    precedence: PrecedenceOption = loader.DEFAULT_PRECEDENCE_FILE,
    genre: Annotated[
        str | None,
        typer.Option("--genre",
                     help="Detected genre, which selects the source "
                     "order.")] = None,
    auto_apply: AutoApplyOption = match_lib.DEFAULT_AUTO_APPLY,
    review_floor: ReviewFloorOption = match_lib.DEFAULT_REVIEW_FLOOR,
) -> None:
  """Shows what one file would be matched to, and why.

  Args:
    file: The audio file to match.
    precedence: Path to precedence.toml.
    genre: Detected genre, selecting the source order.
    auto_apply: Confidence at or above which a match is trusted.
    review_floor: Confidence below which a match is discarded.
  """
  env.load_env()
  precedence_config = _load_precedence_or_exit(precedence)
  available = [item for item in build_all() if item.is_available()]
  try:
    query = probe_lib.query_for_file(file)
  except tag_io.TagError as err:
    typer.echo(f"error: {err}", err=True)
    raise typer.Exit(code=1) from err

  result = match_lib.match_track(query,
                                 available,
                                 precedence_config,
                                 genre=genre,
                                 auto_apply=auto_apply,
                                 review_floor=review_floor)
  typer.echo(f"query: {query.as_text()}")
  typer.echo(f"status: {result.status}  confidence: {result.confidence}")
  if result.is_empty():
    typer.echo("no source returned a usable candidate")
  typer.echo("\ncandidates:")
  for name, candidate in result.candidates.items():
    typer.echo(f"  {name}: {candidate.score.describe()}")
    for reason in candidate.score.reasons:
      typer.echo(f"      - {reason}")
  typer.echo("\nproposed tags:")
  for field, value in result.tags.as_dict().items():
    origin = result.field_sources.get(field, "?")
    typer.echo(f"  {field.ljust(14)} {str(value)[:52].ljust(54)} [{origin}]")
  if result.art_url:
    art_label = "album art".ljust(14)
    typer.echo(f"  {art_label} {result.art_url[:52]}")
  for note in result.notes:
    typer.echo(f"\nnote: {note}")


@match_app.command("summary")
def match_summary(db: DbOption = connection.DEFAULT_DB_FILE) -> None:
  """Summarises how many tracks are in each match state.

  Args:
    db: Path to the SQLite database.
  """
  with connection.open_db(db) as conn:
    counts = queries.match_status_counts(conn)
  if not counts:
    typer.echo("no tracks indexed yet; run `music-match scan` first")
    return
  total = sum(count for _, count in counts)
  typer.echo(f"{total} tracks")
  for status, count in counts:
    typer.echo(f"  {count:6d}  {status}")


@match_app.command("ignore")
def match_ignore(
    file: Annotated[pathlib.Path,
                    typer.Argument(help="Audio file to stop flagging.")],
    db: DbOption = connection.DEFAULT_DB_FILE,
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Why this will never match.")] = None,
) -> None:
  """Marks a track as never going to match.

  For self-made edits and unofficial uploads no public database holds, so
  they stop reappearing in the review queue on every run.

  Args:
    file: The audio file to mark.
    db: Path to the SQLite database.
    reason: An optional note about why.
  """
  with connection.open_db(db) as conn:
    track_id = queries.track_id_for_path(conn, file)
    if track_id is None:
      typer.echo(f"error: {file} is not indexed; run `music-match scan` first",
                 err=True)
      raise typer.Exit(code=1)
    queries.mark_wont_match(conn, track_id, reason)
    conn.commit()
  typer.echo(f"marked as won't match: {file.name}")


ArtStoreOption = Annotated[
    pathlib.Path,
    typer.Option("--art-store", help="Directory holding stored cover art.")]


@app.command("apply")
def apply_matches(
    db: DbOption = connection.DEFAULT_DB_FILE,
    art_store: ArtStoreOption = art_lib.DEFAULT_STORE_DIR,
    source: SourceNameOption = None,
    limit: Annotated[
        int,
        typer.
        Option("--limit", help="Stop after this many tracks. 0 means no limit."
              )] = 0,
    include_review: Annotated[
        bool,
        typer.Option("--include-review",
                     help="Also write matches that were queued for "
                     "review.")] = False,
    skip_art: Annotated[
        bool,
        typer.Option("--skip-art", help="Do not embed cover art.")] = False,
    dry_run: DryRunOption = False,
) -> None:
  """Writes matched metadata into the audio files.

  Every previous value is recorded in `tag_history` *before* the file is
  touched, so `music-match undo` can put any of it back. Only matches the
  matcher trusted are written unless --include-review is given.

  Args:
    db: Path to the SQLite database.
    art_store: Directory holding stored cover art.
    source: Limit to one configured source folder.
    limit: Stop after this many tracks, or 0 for no limit.
    include_review: Also write matches queued for review.
    skip_art: Do not embed cover art.
    dry_run: Report what would be written without touching anything.
  """
  wanted = [match_lib.MatchStatus.MATCHED]
  if include_review:
    wanted.append(match_lib.MatchStatus.REVIEW)
  store = art_lib.ArtStore(art_store)

  with connection.open_db(db) as conn:
    pending = [
        row for row in queries.tracks_with_matches(conn, source)
        if row["match_status"] in wanted
    ]
    if limit:
      pending = pending[:limit]
    typer.echo(f"{len(pending)} track(s) to write "
               f"({_joined(wanted)})")
    if dry_run:
      _report_planned_writes(conn, pending, store, skip_art)
      return
    written, unchanged, failed = _write_all(conn, pending, store, skip_art)

  typer.echo(f"wrote {written}, unchanged {unchanged}, failed {failed}")


def _report_planned_writes(conn: sqlite3.Connection, pending: list[sqlite3.Row],
                           store: art_lib.ArtStore, skip_art: bool) -> None:
  """Prints what a real run would change, touching nothing.

  Args:
    conn: An open database connection.
    pending: The track rows to consider.
    store: Where stored art lives.
    skip_art: Whether art is being skipped.
  """
  total = 0
  for row in pending:
    path = pathlib.Path(row["path"])
    tags = _matched_tags(row)
    if tags is None:
      continue
    try:
      result = apply_lib.apply_tags(conn,
                                    track_id=int(row["id"]),
                                    path=path,
                                    tags=tags,
                                    art_hash=None,
                                    store=store if not skip_art else None,
                                    dry_run=True)
    except (tag_io.TagError, art_lib.ArtError) as err:
      typer.echo(f"  {path.name}: {err}", err=True)
      continue
    planned = sorted(result.changes)
    # A dry run deliberately does not download anything, so it cannot
    # know the new cover's hash — but saying nothing about art would
    # under-report the largest change most files get.
    if not skip_art and row["matched_art_url"]:
      planned.append("album_art (would fetch)")
    if planned:
      total += 1
      typer.echo(f"  {path.name}: {_joined(planned)}")
  typer.echo(f"dry run: would change up to {total} file(s)")


def _write_all(conn: sqlite3.Connection, pending: list[sqlite3.Row],
               store: art_lib.ArtStore, skip_art: bool) -> tuple[int, int, int]:
  """Writes every pending match, recording history first.

  Args:
    conn: An open database connection.
    pending: The track rows to write.
    store: Where stored art lives.
    skip_art: Whether to skip cover art.

  Returns:
    (written, unchanged, failed) counts.
  """
  written = unchanged = failed = 0
  for position, row in enumerate(pending, start=1):
    path = pathlib.Path(row["path"])
    tags = _matched_tags(row)
    if tags is None:
      continue
    try:
      art_hash = None if skip_art else _stored_art(store,
                                                   row["matched_art_url"])
      result = apply_lib.apply_tags(conn,
                                    track_id=int(row["id"]),
                                    path=path,
                                    tags=tags,
                                    art_hash=art_hash,
                                    store=store)
    except (tag_io.TagError, art_lib.ArtError) as err:
      failed += 1
      typer.echo(f"  failed {path.name}: {err}", err=True)
      continue
    if result.wrote:
      written += 1
    else:
      unchanged += 1
    if position % 25 == 0 or position == len(pending):
      typer.echo(f"  {position}/{len(pending)}")
  return (written, unchanged, failed)


def _stored_art(store: art_lib.ArtStore, url: str | None) -> str | None:
  """Fetches and stores a cover image, returning its content hash.

  Args:
    store: Where stored art lives.
    url: The image URL recorded with the match, if any.

  Returns:
    The stored image's hash, or None if there was no URL.

  Raises:
    ArtError: If the image cannot be fetched or stored.
  """
  return store.store_url(url) if url else None


def _matched_tags(row: sqlite3.Row) -> tag_fields.TrackTags | None:
  """Reads the proposed tags recorded against a track.

  Args:
    row: A track row carrying `matched_tags_json`.

  Returns:
    The proposed tags, or None if the row has none or they are corrupt.
  """
  raw = row["matched_tags_json"]
  if not raw:
    return None
  try:
    values = json.loads(raw)
  except json.JSONDecodeError:
    return None
  return tag_fields.TrackTags.from_mapping(values)


@app.command("undo")
def undo(
    file: Annotated[pathlib.Path,
                    typer.Argument(help="Audio file to inspect or revert.")],
    db: DbOption = connection.DEFAULT_DB_FILE,
    art_store: ArtStoreOption = art_lib.DEFAULT_STORE_DIR,
    last: Annotated[
        bool,
        typer.Option("--last", help="Revert the most recent write.")] = False,
    to: Annotated[
        str | None,
        typer.Option("--to", help="Revert to the state before this batch."
                    )] = None,
    dry_run: DryRunOption = False,
) -> None:
  """Shows a file's tag history, or puts an earlier version back.

  With no options this only prints the timeline. Reverting needs --last
  or --to, and the revert is itself recorded, so it can be undone again.

  Args:
    file: The audio file.
    db: Path to the SQLite database.
    art_store: Directory holding stored cover art.
    last: Revert the most recent write.
    to: Revert to the state before this batch.
    dry_run: Report what would be restored without writing.
  """
  with connection.open_db(db) as conn:
    track_id = queries.track_id_for_path(conn, file)
    if track_id is None:
      typer.echo(f"error: {file} is not indexed", err=True)
      raise typer.Exit(code=1)
    batches = queries.batches_for_track(conn, track_id)
    if not batches:
      typer.echo("no recorded changes for this file")
      return

    if not last and to is None:
      _show_history(conn, track_id, batches)
      return

    known = [batch for batch, _, _ in batches]
    if last:
      target = known[0]
    else:
      try:
        target = resolve_batch(str(to), known)
      except LookupError as err:
        typer.echo(f"error: {err}", err=True)
        raise typer.Exit(code=1) from err
    try:
      result = apply_lib.revert(conn,
                                track_id=track_id,
                                path=file,
                                batch=target,
                                store=art_lib.ArtStore(art_store),
                                dry_run=dry_run)
    except (tag_io.TagError, art_lib.ArtError) as err:
      typer.echo(f"error: {err}", err=True)
      raise typer.Exit(code=1) from err

  if result.is_noop():
    typer.echo("nothing to restore; the file already matches that point")
    return
  prefix = "dry run: would restore" if dry_run else "restored"
  typer.echo(f"{prefix} {len(result.changes)} field(s)")
  for field, (old, new) in sorted(result.changes.items()):
    typer.echo(f"  {field.ljust(14)} {_short(old)} -> {_short(new)}")


def resolve_batch(wanted: str, known: list[str]) -> str:
  """Finds a batch from a prefix of its identifier.

  The timeline prints shortened ids because a full one is 32 hex
  characters. Requiring the full id back would mean the value shown can
  never be the value typed.

  Args:
    wanted: The identifier or a prefix of it.
    known: Every batch recorded for this track.

  Returns:
    The full identifier.

  Raises:
    LookupError: If the prefix matches no batch, or more than one.
  """
  matches = [batch for batch in known if batch.startswith(wanted)]
  if not matches:
    raise LookupError(f"no such batch {wanted}")
  if len(matches) > 1:
    raise LookupError(f"{wanted} matches {len(matches)} batches; use more"
                      " characters")
  return matches[0]


def _show_history(conn: sqlite3.Connection, track_id: int,
                  batches: list[tuple[str, str, int]]) -> None:
  """Prints a track's change timeline, newest first.

  Args:
    conn: An open database connection.
    track_id: The track's row id.
    batches: The track's writes, newest first.
  """
  rows = queries.history_for_track(conn, track_id)
  by_batch: dict[str, list[sqlite3.Row]] = {}
  for row in rows:
    by_batch.setdefault(str(row["batch"]), []).append(row)

  typer.echo(f"{len(batches)} recorded change(s), newest first:\n")
  for batch, when, count in batches:
    typer.echo(f"  {batch[:12]}  {when}  ({count} field(s))")
    for row in by_batch.get(batch, []):
      field = str(row["field"]).ljust(14)
      before = _short(row["old_value"])
      after = _short(row["new_value"])
      typer.echo(f"      {field} {before} -> {after}")
  typer.echo("\nRevert with: music-match undo FILE --last"
             " (or --to <batch>)")


def _short(value: object) -> str:
  """Renders a tag value briefly for the timeline.

  Args:
    value: The value, which may be an art content hash.

  Returns:
    A short display form. Hashes are truncated; None reads as "(unset)".
  """
  if value is None:
    return "(unset)"
  text = str(value)
  if len(text) == 64 and all(ch in "0123456789abcdef" for ch in text):
    return f"art:{text[:12]}"
  return text if len(text) <= 40 else text[:37] + "..."


QUARANTINE_SUBFOLDER = "possible-video-rip"


@rips_app.command("list")
def rips_list(
    sources: SourcesOption = loader.DEFAULT_SOURCES_FILE,
    source: SourceNameOption = None,
) -> None:
  """Reports files that look like rips from a music video.

  Only folders with `check_for_video_rips` set are examined. Nothing is
  moved.

  Args:
    sources: Path to sources.toml.
    source: Limit to one configured source folder.
  """
  sources_config = _load_sources_or_exit(sources)
  found = _find_rips(sources_config, source)
  if not found:
    typer.echo("no suspected video rips")
    return
  for item, detection in found:
    typer.echo(f"  {item.path.name}")
    typer.echo(f"      {detection.describe()}")
  typer.echo(f"\n{len(found)} suspected video rip(s)."
             " Quarantine them with: music-match video-rips quarantine --apply")


@rips_app.command("quarantine")
def rips_quarantine(
    sources: SourcesOption = loader.DEFAULT_SOURCES_FILE,
    db: DbOption = connection.DEFAULT_DB_FILE,
    source: SourceNameOption = None,
    apply_moves: Annotated[
        bool,
        typer.Option("--apply",
                     help="Actually move the files. Without this, the scan "
                     "only reports.")] = False,
) -> None:
  """Moves suspected video rips aside for a human to confirm.

  Files go to a `possible-video-rip` folder under the configured review
  path, and are skipped by matching until you put them back. Reports
  without touching anything unless --apply is given.

  Args:
    sources: Path to sources.toml.
    db: Path to the SQLite database.
    source: Limit to one configured source folder.
    apply_moves: Perform the moves rather than only reporting them.
  """
  sources_config = _load_sources_or_exit(sources)
  found = _find_rips(sources_config, source)
  destination = sources_config.review_path / QUARANTINE_SUBFOLDER
  typer.echo(f"{len(found)} suspected video rip(s)")
  if not found:
    return
  if not apply_moves:
    for item, detection in found[:20]:
      typer.echo(f"  {item.path.name}: {detection.describe()}")
    if len(found) > 20:
      typer.echo(f"  ... and {len(found) - 20} more")
    typer.echo(f"reported only. Re-run with --apply to move them to"
               f" {destination}")
    return

  moved = 0
  with connection.open_db(db) as conn:
    for item, _ in found:
      target = destination / item.source.name / item.path.name
      try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
          typer.echo(f"  skipped {item.path.name}: already quarantined",
                     err=True)
          continue
        shutil.move(str(item.path), str(target))
      except OSError as err:
        typer.echo(f"  could not move {item.path.name}: {err}", err=True)
        continue
      track_id = queries.track_id_for_path(conn, item.path)
      if track_id is not None:
        queries.move_track(conn, track_id, target)
        queries.set_match_status(conn, track_id, "quarantined")
        conn.commit()
      moved += 1
  typer.echo(f"quarantined {moved} file(s) to {destination}")


@rips_app.command("restore")
def rips_restore(
    file: Annotated[pathlib.Path,
                    typer.Argument(help="Quarantined file to put back.")],
    sources: SourcesOption = loader.DEFAULT_SOURCES_FILE,
    db: DbOption = connection.DEFAULT_DB_FILE,
) -> None:
  """Returns a quarantined file to its source folder.

  Use once you have confirmed the audio is fine. The track becomes
  eligible for matching again.

  Args:
    file: The quarantined file.
    sources: Path to sources.toml.
    db: Path to the SQLite database.
  """
  sources_config = _load_sources_or_exit(sources)
  quarantine_root = sources_config.review_path / QUARANTINE_SUBFOLDER
  # Restoring is a move *into* the library, so it only accepts files that
  # are actually in quarantine. Without this, any path whose parent
  # happened to be named after a source folder could be moved in.
  if not _is_within(file, quarantine_root):
    typer.echo(f"error: {file} is not in {quarantine_root}", err=True)
    raise typer.Exit(code=1)
  folder = sources_config.folders.get(file.parent.name)
  if folder is None:
    typer.echo(
        f"error: cannot tell which source {file.name} came from."
        f" Expected it under {QUARANTINE_SUBFOLDER}/<source name>/",
        err=True)
    raise typer.Exit(code=1)

  target = folder.path / file.name
  if target.exists():
    typer.echo(f"error: {target} already exists", err=True)
    raise typer.Exit(code=1)
  try:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(file), str(target))
  except OSError as err:
    typer.echo(f"error: {err}", err=True)
    raise typer.Exit(code=1) from err

  with connection.open_db(db) as conn:
    track_id = queries.track_id_for_path(conn, file)
    if track_id is not None:
      queries.move_track(conn, track_id, target)
      queries.set_match_status(conn, track_id, "pending")
      conn.commit()
  typer.echo(f"restored {file.name} to {folder.path}")


def _is_within(path: pathlib.Path, root: pathlib.Path) -> bool:
  """Returns whether a path lies inside a directory.

  Args:
    path: The path to test.
    root: The directory it should be under.

  Returns:
    True if `path` is inside `root`.
  """
  try:
    path.resolve().relative_to(root.resolve())
  except ValueError:
    return False
  return True


def _find_rips(
    sources_config: loader.SourcesConfig, source: str | None
) -> list[tuple[library.LibraryFile, videorip_lib.Detection]]:
  """Finds suspected video rips in the configured source folders.

  Args:
    sources_config: The loaded source configuration.
    source: Limit to one configured source folder.

  Returns:
    Each suspected file with the detection that flagged it.

  Raises:
    typer.Exit: If a named source is unknown or a folder is missing.
  """
  try:
    files = list(library.walk(sources_config, source))
  except KeyError:
    typer.echo(f"error: no source folder named '{source}'", err=True)
    raise typer.Exit(code=1) from None
  except FileNotFoundError as err:
    typer.echo(f"error: {err}", err=True)
    raise typer.Exit(code=1) from err

  found = []
  for item in files:
    if not item.source.check_for_video_rips:
      continue
    try:
      title = tag_io.read_tags(item.path).title
    except tag_io.TagError:
      title = None
    detection = videorip_lib.detect(item.path, title)
    if detection.is_rip:
      found.append((item, detection))
  return found


@app.command("intake")
def intake(
    links: Annotated[list[str] | None,
                     typer.Argument(
                         help="Links to download. Albums and playlists are "
                         "expanded.")] = None,
    from_file: Annotated[
        pathlib.Path | None,
        typer.Option("--from-file",
                     help="Read links from a file, one per line. Blank "
                     "lines and # comments are ignored.")] = None,
    sources: SourcesOption = loader.DEFAULT_SOURCES_FILE,
    db: DbOption = connection.DEFAULT_DB_FILE,
    into: Annotated[
        str,
        typer.Option("--into",
                     help="Which configured source folder to download "
                     "into.")] = "yt-dlp",
    assume_new: Annotated[
        bool,
        typer.Option("--assume-new",
                     help="Do not ask about possible duplicates; download "
                     "them. For unattended runs.")] = False,
    dry_run: DryRunOption = False,
) -> None:
  """Downloads tracks from submitted links.

  Runs the two pre-download dedup layers first: anything already in the
  archive is skipped outright, and anything that merely *looks* like a
  track you already have raises a question rather than being skipped —
  a shared title is often a different mix.

  Args:
    links: Links to download.
    from_file: Read links from a file instead of, or as well as,
      arguments.
    sources: Path to sources.toml.
    db: Path to the SQLite database.
    into: Which configured source folder to download into.
    assume_new: Download possible duplicates without asking.
    dry_run: Report what would be downloaded without fetching anything.
  """
  sources_config = _load_sources_or_exit(sources)
  folder = sources_config.folders.get(into)
  if folder is None:
    typer.echo(
        f"error: no source folder named '{into}'."
        f" Known: {_joined(sources_config.folders)}",
        err=True)
    raise typer.Exit(code=1)

  submitted = _submitted_links(links, from_file)
  if not submitted:
    typer.echo("nothing submitted: give links, or --from-file", err=True)
    raise typer.Exit(code=1)

  typer.echo(f"expanding {len(submitted)} link(s)...")
  try:
    entries = intake_lib.expand(submitted)
  except intake_lib.IntakeError as err:
    typer.echo(f"error: {err}", err=True)
    raise typer.Exit(code=1) from err
  typer.echo(f"{len(entries)} track(s) found")

  with connection.open_db(db) as conn:
    wanted = _after_dedup(conn, entries, assume_new)
    if dry_run:
      typer.echo(f"dry run: would download {len(wanted)} track(s) into"
                 f" {folder.path}")
      for entry in wanted:
        typer.echo(f"  {entry.label()}")
      return
    _download_all(conn, wanted, folder)


def _submitted_links(links: list[str] | None,
                     from_file: pathlib.Path | None) -> list[str]:
  """Collects links from arguments and from a file.

  Args:
    links: Links given as arguments.
    from_file: A file of links, one per line.

  Returns:
    Every link, in order, deduplicated.

  Raises:
    typer.Exit: If the file cannot be read.
  """
  text = "\n".join(links or [])
  if from_file is not None:
    try:
      text += "\n" + from_file.read_text(encoding="utf-8")
    except OSError as err:
      typer.echo(f"error: could not read {from_file}: {err}", err=True)
      raise typer.Exit(code=1) from err
  return intake_lib.parse_links(text)


def _after_dedup(conn: sqlite3.Connection, entries: list[intake_lib.Entry],
                 assume_new: bool) -> list[intake_lib.Entry]:
  """Applies both pre-download dedup layers.

  Args:
    conn: An open database connection.
    entries: Every expanded entry.
    assume_new: Skip the confirmation prompt and keep everything.

  Returns:
    The entries still worth downloading.
  """
  wanted = []
  archived = 0
  for entry in entries:
    if intake_lib.in_archive(conn, entry):
      archived += 1
      continue
    candidates = intake_lib.find_candidates(conn, entry)
    if candidates and not assume_new and not _confirm_download(
        entry, candidates):
      continue
    wanted.append(entry)
  if archived:
    typer.echo(f"{archived} already downloaded, skipped")
  return wanted


def _confirm_download(entry: intake_lib.Entry,
                      candidates: list[intake_lib.Candidate]) -> bool:
  """Asks whether an entry that looks familiar should still be fetched.

  Deliberately a question rather than a decision. A shared title is often
  a legitimately different mix, and silently skipping a track that was
  wanted is worse than fetching one already held — the fingerprint layer
  catches a true duplicate after download anyway.

  Args:
    entry: The entry in question.
    candidates: What it might duplicate.

  Returns:
    True if it should be downloaded.
  """
  typer.echo(f"\n{entry.label()} may already be in the library:")
  for candidate in candidates[:3]:
    typer.echo(f"    {candidate.describe()}")
  return typer.confirm("  download it anyway?", default=True)


def _download_all(conn: sqlite3.Connection, entries: list[intake_lib.Entry],
                  folder: loader.SourceFolder) -> None:
  """Downloads entries and records each one as it completes.

  Args:
    conn: An open database connection.
    entries: What to download.
    folder: The source folder to download into.
  """
  if not entries:
    typer.echo("nothing new to download")
    return
  done = failed = 0
  for position, entry in enumerate(entries, start=1):
    typer.echo(f"  [{position}/{len(entries)}] {entry.label()}")
    try:
      result = intake_lib.download_entry(entry, folder.path)
    except intake_lib.IntakeError as err:
      failed += 1
      typer.echo(f"      failed: {err}", err=True)
      continue
    track_id = queries.upsert_track(conn,
                                    path=result.path,
                                    source_name=folder.name)
    # Recorded only after the file is on disk, so an interrupted run
    # retries the download rather than skipping it forever.
    intake_lib.record_download(conn, entry, track_id)
    conn.commit()
    done += 1
    if not result.stamped:
      typer.echo("      note: could not write the source id into the file",
                 err=True)
  typer.echo(f"downloaded {done}, failed {failed}")
