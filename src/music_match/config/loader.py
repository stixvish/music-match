"""Loads and validates the two TOML config files.

`sources.toml` says which folders to scan and how to treat them.
`precedence.toml` says which metadata sources to query, in what order,
for a locally-detected genre — optionally overridden per field.

The two are deliberately separate concerns and are loaded independently;
nothing here reads one to make sense of the other.
"""

import dataclasses
import pathlib
import re
import tomllib
from typing import Any, Mapping

DEFAULT_SOURCES_FILE = pathlib.Path("sources.toml")
DEFAULT_PRECEDENCE_FILE = pathlib.Path("precedence.toml")

DEFAULT_GENRE_KEY = "default"


class ConfigError(Exception):
  """Raised when a config file is missing, malformed, or incomplete."""


@dataclasses.dataclass(frozen=True)
class SourceFolder:
  """One configured source folder.

  Attributes:
    name: The folder's name, e.g. "yt-dlp". This is the identity used for
      matching — never the absolute path, so the library can move.
    path: Where the folder currently lives, with `~` expanded.
    check_for_video_rips: Whether files here should go through video-rip
      detection during tagging.
  """
  name: str
  path: pathlib.Path
  check_for_video_rips: bool


@dataclasses.dataclass(frozen=True)
class SourcesConfig:
  """All configured source folders, keyed by name."""
  folders: Mapping[str, SourceFolder]

  def names(self) -> tuple[str, ...]:
    """Returns the configured folder names, in config order."""
    return tuple(self.folders)

  def folder_for_path(self, path: pathlib.Path) -> SourceFolder | None:
    """Finds the source folder a file belongs to, matching by name.

    Matching walks the path's components looking for a configured folder
    name rather than comparing against the configured absolute path, so a
    library that has moved to another drive still resolves correctly.

    Args:
      path: Any path, absolute or relative, to a file or directory.

    Returns:
      The matching SourceFolder, or None if no component matches a
      configured name.
    """
    for part in reversed(path.parts):
      folder = self.folders.get(part)
      if folder is not None:
        return folder
    return None

  def contains(self, path: pathlib.Path) -> bool:
    """Returns whether a path falls inside some configured source folder.

    Args:
      path: The path to test.

    Returns:
      True if the path is under a configured source folder. Anything that
      returns False must never be touched.
    """
    return self.folder_for_path(path) is not None


@dataclasses.dataclass(frozen=True)
class GenrePrecedence:
  """Source ordering for one genre.

  Attributes:
    order: Default source order for this genre.
    fields: Per-field overrides. Fields absent here use `order`.
  """
  order: tuple[str, ...]
  fields: Mapping[str, tuple[str, ...]]


@dataclasses.dataclass(frozen=True)
class PrecedenceConfig:
  """Source precedence for every configured genre."""
  genres: Mapping[str, GenrePrecedence]

  def order_for(self,
                genre: str | None,
                field: str | None = None) -> tuple[str, ...]:
    """Returns the source query order for a genre, optionally per field.

    Falls back to the `default` genre when the genre is unknown or absent,
    and to the genre's own `order` when the field has no override.

    Args:
      genre: A locally-detected genre, in any form the detector emits —
        it is normalized before lookup. None uses the default entry.
      field: A target metadata field name, e.g. "remixer". None returns
        the genre's default order.

    Returns:
      Source names in the order they should be queried.
    """
    entry = self.genres.get(normalize_genre(genre) if genre else "")
    if entry is None:
      entry = self.genres[DEFAULT_GENRE_KEY]
    if field is not None:
      override = entry.fields.get(field)
      if override is not None:
        return override
    return entry.order

  def source_names(self) -> tuple[str, ...]:
    """Returns every source named anywhere in the config, deduplicated."""
    seen: dict[str, None] = {}
    for entry in self.genres.values():
      for name in entry.order:
        seen[name] = None
      for override in entry.fields.values():
        for name in override:
          seen[name] = None
    return tuple(seen)


def normalize_genre(genre: str) -> str:
  """Reduces a detected genre to a precedence.toml key.

  The Essentia discogs-effnet model emits labels like
  "Electronic---Deep House"; precedence is keyed on the top-level genre.

  Args:
    genre: A raw genre label from a detector or a tag.

  Returns:
    A lowercase key with the sub-genre dropped, "&" spelled as "n", spaces
    and hyphens turned into underscores, and other punctuation removed —
    "Electronic---Deep House" becomes "electronic", "R&B" becomes "rnb",
    and "Drum & Bass" becomes "drum_n_bass".
  """
  top_level = genre.split("---")[0].strip().lower().replace("&", "n")
  underscored = re.sub(r"[\s\-]+", "_", top_level)
  return re.sub(r"[^a-z0-9_]", "", underscored)


def _read_toml(path: pathlib.Path) -> dict[str, Any]:
  """Reads and parses a TOML file.

  Args:
    path: Path to the file.

  Returns:
    The parsed document.

  Raises:
    ConfigError: If the file is missing or is not valid TOML.
  """
  try:
    with path.open("rb") as handle:
      return tomllib.load(handle)
  except FileNotFoundError as err:
    raise ConfigError(f"config file not found: {path}") from err
  except tomllib.TOMLDecodeError as err:
    raise ConfigError(f"{path} is not valid TOML: {err}") from err


def _require_str_list(value: Any, where: str) -> tuple[str, ...]:
  """Validates that a config value is a non-empty list of strings.

  Args:
    value: The raw value from the parsed TOML.
    where: Human-readable location, used in the error message.

  Returns:
    The value as a tuple of strings.

  Raises:
    ConfigError: If the value is not a non-empty list of strings.
  """
  if not isinstance(value, list) or not value:
    raise ConfigError(f"{where} must be a non-empty list of source names")
  for item in value:
    if not isinstance(item, str):
      raise ConfigError(f"{where} must contain only strings, got {item!r}")
  return tuple(value)


def load_sources(path: pathlib.Path = DEFAULT_SOURCES_FILE) -> SourcesConfig:
  """Loads sources.toml.

  Args:
    path: Path to the config file.

  Returns:
    The parsed source folder configuration.

  Raises:
    ConfigError: If the file is missing, malformed, or declares no
      folders.
  """
  document = _read_toml(path)
  raw_sources = document.get("sources")
  if not isinstance(raw_sources, dict) or not raw_sources:
    raise ConfigError(f"{path} declares no [sources.<name>] tables")

  folders: dict[str, SourceFolder] = {}
  for name, raw in raw_sources.items():
    if not isinstance(raw, dict):
      raise ConfigError(f"[sources.{name}] must be a table")
    raw_path = raw.get("path")
    if not isinstance(raw_path, str) or not raw_path:
      raise ConfigError(f"[sources.{name}] needs a non-empty `path`")
    check = raw.get("check_for_video_rips", False)
    if not isinstance(check, bool):
      raise ConfigError(
          f"[sources.{name}].check_for_video_rips must be true or false")
    folders[name] = SourceFolder(
        name=name,
        path=pathlib.Path(raw_path).expanduser(),
        check_for_video_rips=check,
    )
  return SourcesConfig(folders=folders)


def load_precedence(
    path: pathlib.Path = DEFAULT_PRECEDENCE_FILE) -> PrecedenceConfig:
  """Loads precedence.toml.

  Args:
    path: Path to the config file.

  Returns:
    The parsed precedence configuration.

  Raises:
    ConfigError: If the file is missing, malformed, or has no
      [genres.default] entry to fall back to.
  """
  document = _read_toml(path)
  raw_genres = document.get("genres")
  if not isinstance(raw_genres, dict) or not raw_genres:
    raise ConfigError(f"{path} declares no [genres.<name>] tables")

  genres: dict[str, GenrePrecedence] = {}
  for name, raw in raw_genres.items():
    if not isinstance(raw, dict):
      raise ConfigError(f"[genres.{name}] must be a table")
    order = _require_str_list(raw.get("order"), f"[genres.{name}].order")
    raw_fields = raw.get("fields", {})
    if not isinstance(raw_fields, dict):
      raise ConfigError(f"[genres.{name}.fields] must be a table")
    fields = {
        field: _require_str_list(value, f"[genres.{name}.fields].{field}")
        for field, value in raw_fields.items()
    }
    genres[normalize_genre(name)] = GenrePrecedence(order=order, fields=fields)

  if DEFAULT_GENRE_KEY not in genres:
    raise ConfigError(
        f"{path} needs a [genres.{DEFAULT_GENRE_KEY}] entry to fall back to")
  return PrecedenceConfig(genres=genres)
