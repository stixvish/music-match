"""Runtime configuration.

Credentials are read from the environment first (populated by a gitignored
`.env`), falling back to `~/.config/musicpipeline/config.toml`. Neither ever
enters the repository — see SPEC.md §13.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_HOME = Path.home() / ".config" / "musicpipeline"
DEFAULT_CONFIG = CONFIG_HOME / "config.toml"

# Every credential the pipeline knows about. A missing one disables its
# source rather than failing the run (SPEC.md §13).
CREDENTIAL_KEYS = (
  "ACOUSTID_API_KEY",
  "DISCOGS_TOKEN",
  "SPOTIFY_CLIENT_ID",
  "SPOTIFY_CLIENT_SECRET",
  "BEATPORT_CLIENT_ID",
  "BEATPORT_CLIENT_SECRET",
)


def _load_dotenv(path: Path) -> None:
  """Load KEY=VALUE pairs from a .env file into os.environ.

  Existing environment variables win, so a real export overrides the file.

  Args:
    path: Location of the .env file. Missing files are ignored.
  """
  if not path.is_file():
    return
  for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue
    key, _, value = line.partition("=")
    os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class YouTubeConfig:
  """How audio is fetched. See SPEC.md §6."""

  cookie_browser: str = "chrome"
  # Path to a cookies.txt exported from a *private* YouTube session. Preferred
  # over reading the live browser profile: YouTube rotates account cookies on
  # open tabs as a security measure, so ordinary browsing invalidates whatever
  # was exported from that profile. A private session that is closed and never
  # reopened has nothing to rotate it (SPEC.md §6).
  cookie_file: str = ""
  # Which browser profile the jar is bootstrapped and refreshed from. The
  # default profile is the one you are signed into and browsing in, so YouTube
  # rotates its session and de-authenticates whatever was exported from it.
  # A profile that is signed in and then left closed has nothing rotating it.
  cookie_profile: str = ""
  # 141 (aac 44.1k) is preferred over 774 (opus 48k, resampled). SPEC.md §4.
  format_chain: str = "141/774/140/251"
  player_client: str = "web_music"
  min_bitrate_kbps: int = 256


@dataclass(frozen=True)
class Paths:
  """Where things live on disk."""

  library: Path = Path.home() / "Music" / "library"
  staging: Path = Path.home() / "Music" / ".staging"
  database: Path = Path.home() / ".local" / "share" / "musicpipeline" / "music.db"


@dataclass(frozen=True)
class Config:
  """Top-level configuration."""

  youtube: YouTubeConfig = field(default_factory=YouTubeConfig)
  paths: Paths = field(default_factory=Paths)
  credentials: dict[str, str] = field(default_factory=dict)

  def require(self, key: str) -> str:
    """Return a credential, or raise with a pointer to where it comes from.

    Args:
      key: Environment-style credential name, e.g. `ACOUSTID_API_KEY`.

    Returns:
      The credential value.

    Raises:
      RuntimeError: If the credential is absent.
    """
    value = self.credentials.get(key) or os.environ.get(key, "")
    if not value:
      raise RuntimeError(
        f"missing credential {key}. add it to .env or {DEFAULT_CONFIG}"
      )
    return value


def _expand(value: str) -> Path:
  return Path(value).expanduser()


def load(config_path: Path | None = None, env_path: Path | None = None) -> Config:
  """Build the runtime configuration.

  Args:
    config_path: Override for the TOML config location.
    env_path: Override for the .env location.

  Returns:
    A populated Config.
  """
  _load_dotenv(env_path or Path.cwd() / ".env")

  data: dict[str, object] = {}
  path = config_path or DEFAULT_CONFIG
  if path.is_file():
    data = tomllib.loads(path.read_text(encoding="utf-8"))

  yt_raw = data.get("youtube", {})
  yt = YouTubeConfig(**yt_raw) if isinstance(yt_raw, dict) else YouTubeConfig()

  paths_raw = data.get("paths", {})
  paths = (
    Paths(**{k: _expand(str(v)) for k, v in paths_raw.items()})
    if isinstance(paths_raw, dict) and paths_raw
    else Paths()
  )

  # The environment wins, then `[credentials]` in the TOML. Both were always
  # documented; only the first was ever implemented, so a user who followed the
  # README and moved their keys to `~/.config/musicpipeline/config.toml` lost
  # every optional source with no error — each one degrades silently by design.
  #
  # The TOML matters because `.env` is read from the *working directory*: the
  # same command run from another folder would quietly lose its credentials.
  from_toml = data.get("credentials", {})
  if not isinstance(from_toml, dict):
    from_toml = {}
  creds = {
    k: str(os.environ.get(k) or from_toml.get(k) or "")
    for k in CREDENTIAL_KEYS
    if os.environ.get(k) or from_toml.get(k)
  }
  return Config(youtube=yt, paths=paths, credentials=creds)
