"""The `music-match` command-line entry point.

Only the skeleton commands exist so far: inspecting resolved config,
creating the database, and dumping a file's tags. Pipeline commands land
with the pipeline stages they belong to.
"""

import pathlib
from typing import Annotated, Iterable

import typer

from music_match import __version__
from music_match.config import loader
from music_match.db import connection
from music_match.db import schema
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
