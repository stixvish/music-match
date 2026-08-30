"""Tests for TOML config loading."""

import pathlib

import pytest

from music_match.config import loader


def write(tmp_path: pathlib.Path, name: str, body: str) -> pathlib.Path:
  """Writes a config file into a temporary directory.

  Args:
    tmp_path: The temporary directory.
    name: File name to write.
    body: File contents.

  Returns:
    Path to the written file.
  """
  path = tmp_path / name
  path.write_text(body, encoding="utf-8")
  return path


def test_load_sources_expands_home(tmp_path: pathlib.Path) -> None:
  """A `~` in a configured path is expanded at load time."""
  path = write(
      tmp_path, "sources.toml", """
      [sources.yt-dlp]
      path = "~/Music/yt-dlp"
      check_for_video_rips = true
      """)
  config = loader.load_sources(path)
  folder = config.folders["yt-dlp"]
  assert folder.path == pathlib.Path.home() / "Music/yt-dlp"
  assert folder.check_for_video_rips is True


def test_load_sources_defaults_video_rip_check_off(
    tmp_path: pathlib.Path) -> None:
  """Omitting check_for_video_rips defaults it to False."""
  path = write(tmp_path, "sources.toml",
               '[sources.beatport]\npath = "/music/beatport"\n')
  assert config_folder(path).check_for_video_rips is False


def config_folder(path: pathlib.Path) -> loader.SourceFolder:
  """Loads a single-folder sources file and returns that folder.

  Args:
    path: Path to the sources file.

  Returns:
    The only configured folder.
  """
  config = loader.load_sources(path)
  return next(iter(config.folders.values()))


def test_folder_matching_is_by_name_not_path(tmp_path: pathlib.Path) -> None:
  """A library that has moved drives still resolves to its source folder."""
  path = write(
      tmp_path, "sources.toml", """
      [sources.yt-dlp]
      path = "~/Music/yt-dlp"
      [sources.beatport]
      path = "~/Music/beatport"
      """)
  config = loader.load_sources(path)

  moved = pathlib.Path("/Volumes/External/Archive/yt-dlp/Artist - Track.m4a")
  assert config.folder_for_path(moved) is not None
  assert config.folder_for_path(moved).name == "yt-dlp"
  assert config.contains(moved)


def test_folder_matching_rejects_outside_paths(tmp_path: pathlib.Path) -> None:
  """Anything not under a configured folder name is never matched."""
  path = write(tmp_path, "sources.toml", '[sources.yt-dlp]\npath = "~/x"\n')
  config = loader.load_sources(path)
  outside = pathlib.Path("/Users/someone/Downloads/track.m4a")
  assert config.folder_for_path(outside) is None
  assert not config.contains(outside)


def test_nested_folder_name_matches_deepest_first(
    tmp_path: pathlib.Path) -> None:
  """When two configured names appear in one path, the deepest one wins."""
  path = write(
      tmp_path, "sources.toml", """
      [sources.yt-dlp]
      path = "~/Music/yt-dlp"
      [sources.beatport]
      path = "~/Music/yt-dlp/beatport"
      """)
  config = loader.load_sources(path)
  nested = pathlib.Path("/Music/yt-dlp/beatport/track.wav")
  assert config.folder_for_path(nested).name == "beatport"


@pytest.mark.parametrize("body,message", [
    ("", "declares no"),
    ("[sources.a]\n", "non-empty `path`"),
    ('[sources.a]\npath = ""\n', "non-empty `path`"),
    ('[sources.a]\npath = "/x"\ncheck_for_video_rips = "yes"\n',
     "true or false"),
])
def test_load_sources_rejects_bad_config(tmp_path: pathlib.Path, body: str,
                                         message: str) -> None:
  """Malformed source config raises ConfigError, not a bare KeyError."""
  path = write(tmp_path, "sources.toml", body)
  with pytest.raises(loader.ConfigError, match=message):
    loader.load_sources(path)


def test_load_sources_missing_file(tmp_path: pathlib.Path) -> None:
  """A missing config file names the file it looked for."""
  with pytest.raises(loader.ConfigError, match="not found"):
    loader.load_sources(tmp_path / "nope.toml")


def test_load_sources_rejects_invalid_toml(tmp_path: pathlib.Path) -> None:
  """Invalid TOML is reported as a config error."""
  path = write(tmp_path, "sources.toml", "[sources.a\n")
  with pytest.raises(loader.ConfigError, match="not valid TOML"):
    loader.load_sources(path)


@pytest.mark.parametrize("raw,expected", [
    ("Electronic---Deep House", "electronic"),
    ("Electronic", "electronic"),
    ("R&B", "rnb"),
    ("Hip Hop", "hip_hop"),
    ("  Drum-n-Bass  ", "drum_n_bass"),
    ("Drum & Bass", "drum_bass"),
    ("Funk / Soul", "funk_soul"),
    ("Children's", "childrens"),
    ("Folk, World, & Country", "folk_world_country"),
])
def test_normalize_genre(raw: str, expected: str) -> None:
  """Detector labels reduce to precedence.toml keys."""
  assert loader.normalize_genre(raw) == expected


def precedence_file(tmp_path: pathlib.Path) -> pathlib.Path:
  """Writes a precedence file covering both genre and field ordering.

  Args:
    tmp_path: The temporary directory.

  Returns:
    Path to the written file.
  """
  return write(
      tmp_path, "precedence.toml", """
      [genres.default]
      order = ["musicbrainz", "spotify"]

      [genres.electronic]
      order = ["discogs", "musicbrainz"]

      [genres.electronic.fields]
      isrc = ["musicbrainz", "spotify"]
      """)


def test_order_for_uses_genre_then_field_override(
    tmp_path: pathlib.Path) -> None:
  """Field overrides win over the genre order; other fields fall back."""
  config = loader.load_precedence(precedence_file(tmp_path))
  assert config.order_for("Electronic---Deep House") == ("discogs",
                                                         "musicbrainz")
  assert config.order_for("Electronic", "isrc") == ("musicbrainz", "spotify")
  assert config.order_for("Electronic", "remixer") == ("discogs", "musicbrainz")


def test_order_for_falls_back_to_default(tmp_path: pathlib.Path) -> None:
  """An unknown or absent genre uses the default entry."""
  config = loader.load_precedence(precedence_file(tmp_path))
  assert config.order_for("Polka") == ("musicbrainz", "spotify")
  assert config.order_for(None) == ("musicbrainz", "spotify")
  assert config.order_for("Polka", "isrc") == ("musicbrainz", "spotify")


def test_source_names_deduplicates(tmp_path: pathlib.Path) -> None:
  """Every source named anywhere is listed once, in first-seen order."""
  config = loader.load_precedence(precedence_file(tmp_path))
  assert config.source_names() == ("musicbrainz", "spotify", "discogs")


def test_precedence_requires_default(tmp_path: pathlib.Path) -> None:
  """Without a default entry there is nothing to fall back to."""
  path = write(tmp_path, "precedence.toml",
               '[genres.electronic]\norder = ["discogs"]\n')
  with pytest.raises(loader.ConfigError, match="genres.default"):
    loader.load_precedence(path)


@pytest.mark.parametrize("body", [
    "[genres.default]\norder = []\n",
    "[genres.default]\norder = \"discogs\"\n",
    "[genres.default]\norder = [1, 2]\n",
])
def test_precedence_rejects_bad_order(tmp_path: pathlib.Path,
                                      body: str) -> None:
  """An order must be a non-empty list of source-name strings."""
  path = write(tmp_path, "precedence.toml", body)
  with pytest.raises(loader.ConfigError):
    loader.load_precedence(path)


def test_repo_config_files_load() -> None:
  """The config files committed to this repo are themselves valid."""
  sources = loader.load_sources(loader.DEFAULT_SOURCES_FILE)
  precedence = loader.load_precedence(loader.DEFAULT_PRECEDENCE_FILE)
  assert "yt-dlp" in sources.folders
  assert precedence.order_for("Electronic")[0] == "discogs"


def test_duplicates_path_defaults(tmp_path: pathlib.Path) -> None:
  """Omitting [duplicates] still yields a usable destination."""
  path = write(tmp_path, "sources.toml", '[sources.a]\npath = "/music/a"\n')
  assert loader.load_sources(path).duplicates_path.is_absolute()


def test_duplicates_path_is_expanded(tmp_path: pathlib.Path) -> None:
  """A `~` in the duplicates path is expanded like any other path."""
  path = write(
      tmp_path, "sources.toml",
      '[sources.a]\npath = "/music/a"\n\n[duplicates]\npath = "~/dupes"\n')
  assert loader.load_sources(
      path).duplicates_path == pathlib.Path.home() / "dupes"


def test_duplicates_path_may_not_sit_inside_a_source_folder(
    tmp_path: pathlib.Path) -> None:
  """Otherwise the next scan re-indexes everything dedup just moved out."""
  path = write(
      tmp_path, "sources.toml", """
      [sources.yt-dlp]
      path = "~/Music/yt-dlp"

      [duplicates]
      path = "~/Music/yt-dlp/_dupes"
      """)
  with pytest.raises(loader.ConfigError, match="inside a configured"):
    loader.load_sources(path)


def test_duplicates_path_rejects_a_bad_type(tmp_path: pathlib.Path) -> None:
  """A non-string duplicates path is a config error."""
  path = write(tmp_path, "sources.toml",
               '[sources.a]\npath = "/music/a"\n\n[duplicates]\npath = 3\n')
  with pytest.raises(loader.ConfigError, match="non-empty string"):
    loader.load_sources(path)
