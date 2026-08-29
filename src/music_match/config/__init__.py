"""Configuration loading for sources.toml and precedence.toml."""

from music_match.config.loader import ConfigError
from music_match.config.loader import GenrePrecedence
from music_match.config.loader import PrecedenceConfig
from music_match.config.loader import SourceFolder
from music_match.config.loader import SourcesConfig
from music_match.config.loader import load_precedence
from music_match.config.loader import load_sources
from music_match.config.loader import normalize_genre

__all__ = [
    "ConfigError",
    "GenrePrecedence",
    "PrecedenceConfig",
    "SourceFolder",
    "SourcesConfig",
    "load_precedence",
    "load_sources",
    "normalize_genre",
]
