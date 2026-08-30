"""The `music-match` command-line entry point.

Commands land with the pipeline stage they belong to. So far: inspecting
resolved config, creating the database, dumping a file's tags,
fingerprinting the library, and the duplicate scan.
"""

import pathlib
import shutil
import sqlite3
from typing import Annotated, Iterable

import requests
import typer

from music_match import __version__
from music_match import library
from music_match import probe as probe_lib
from music_match.config import env
from music_match.config import loader
from music_match.db import connection
from music_match.db import queries
from music_match.db import schema
from music_match.tagging import dedup as dedup_lib
from music_match.tagging import fingerprint as fp
from music_match.tagging import genre as genre_lib
from music_match.tagging import quality as quality_lib
from music_match.sources import SOURCE_TYPES
from music_match.sources import base as source_base
from music_match.sources import build_all
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
genre_app = typer.Typer(help="Local genre detection.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(tags_app, name="tags")
app.add_typer(genre_app, name="genre")

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
