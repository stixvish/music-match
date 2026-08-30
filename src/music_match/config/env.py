"""Loading credentials from `.env`.

Kept separate from the TOML config because these are secrets: they live in
a gitignored `.env`, never in `sources.toml` or `precedence.toml`.

`dotenv.load_dotenv()` with no argument searches upward from the *calling
module's* directory, which for an installed console script is somewhere
inside `.venv` — it would never find the project's `.env`. Everything here
resolves from the working directory instead.
"""

import os
import pathlib

import dotenv

DEFAULT_ENV_FILE = pathlib.Path(".env")


class MissingCredential(Exception):
  """Raised when a source is asked to run without the keys it needs."""


def load_env(path: pathlib.Path | None = None) -> bool:
  """Loads environment variables from a `.env` file.

  Values already present in the environment win, so an exported variable
  overrides the file.

  Args:
    path: The file to read. Defaults to `.env` in the working directory.

  Returns:
    True if a file was found and read.
  """
  target = path or DEFAULT_ENV_FILE
  if not target.is_file():
    return False
  return dotenv.load_dotenv(dotenv_path=target, override=False)


def get(name: str, default: str | None = None) -> str | None:
  """Reads an environment variable.

  Args:
    name: The variable name.
    default: What to return when it is unset or empty.

  Returns:
    The value, or `default` if it is unset or blank.
  """
  value = os.environ.get(name)
  return value.strip() if value and value.strip() else default


def require(name: str, source: str) -> str:
  """Reads an environment variable that a source cannot work without.

  Args:
    name: The variable name.
    source: The source needing it, used in the error message.

  Returns:
    The value.

  Raises:
    MissingCredential: If the variable is unset or blank.
  """
  value = get(name)
  if value is None:
    raise MissingCredential(
        f"{source} needs {name}. Copy .env.example to .env and fill it in.")
  return value


def has(*names: str) -> bool:
  """Returns whether every named variable is set and non-blank.

  Args:
    *names: The variable names to check.

  Returns:
    True if all of them are present.
  """
  return all(get(name) is not None for name in names)
