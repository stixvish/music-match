"""Album art: fetching, normalising, and content-addressed storage.

Art is stored as files under `.music-match/art-store/`, named by the
SHA-256 of their bytes, and `tag_history` records only the hash. Keeping
images out of the database is deliberate: at 50-150KB per cover, across a
few thousand tracks and however many revisions each accumulates, BLOB
columns would bloat the database and slow every query that touches that
table — including ones with nothing to do with art.

Content addressing also deduplicates for free. Every track on an album
embeds the same cover, so they share one stored file rather than a dozen
copies of the same image.
"""

import dataclasses
import hashlib
import io
import pathlib

import requests
from PIL import Image
from PIL.Image import Resampling

from music_match.sources import http

DEFAULT_STORE_DIR = pathlib.Path(".music-match/art-store")

# What Rekordbox wants embedded, and what ARCHITECTURE specifies.
TARGET_SIZE = 640

# JPEG quality for the normalised copy. High enough that re-encoding is
# not visible at this size, low enough to keep covers around 100KB.
JPEG_QUALITY = 90

_DOWNLOAD_TIMEOUT_SECONDS = 30
# Refuse anything implausible for a cover image, so a redirect to an HTML
# error page cannot end up embedded in a music file.
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


class ArtError(Exception):
  """Raised when cover art cannot be fetched, decoded or stored."""


def digest(data: bytes) -> str:
  """Returns the content address of some image bytes.

  Args:
    data: The image bytes.

  Returns:
    Their SHA-256, as hex.
  """
  return hashlib.sha256(data).hexdigest()


def normalise(data: bytes, size: int = TARGET_SIZE) -> bytes:
  """Converts an image to the square JPEG this tool embeds.

  Args:
    data: The original image bytes, in any format Pillow reads.
    size: The edge length to produce.

  Returns:
    JPEG bytes, `size` by `size`.

  Raises:
    ArtError: If the bytes are not a readable image.
  """
  try:
    with Image.open(io.BytesIO(data)) as image:
      converted = image.convert("RGB")
      resized = converted.resize((size, size), Resampling.LANCZOS)
      buffer = io.BytesIO()
      resized.save(buffer, format="JPEG", quality=JPEG_QUALITY)
      return buffer.getvalue()
  except (OSError, ValueError) as err:
    raise ArtError(f"could not read image: {err}") from err


def fetch(url: str, *, timeout: int = _DOWNLOAD_TIMEOUT_SECONDS) -> bytes:
  """Downloads an image.

  Args:
    url: Where to fetch it from.
    timeout: Seconds to wait.

  Returns:
    The raw image bytes.

  Raises:
    ArtError: If the download fails or returns something implausible.
  """
  try:
    response = requests.get(url, timeout=timeout, stream=True)
    response.raise_for_status()
    data = response.raw.read(_MAX_DOWNLOAD_BYTES + 1, decode_content=True)
  except (requests.RequestException, http.HttpError) as err:
    raise ArtError(f"could not download {url}: {err}") from err
  if not data:
    raise ArtError(f"{url} returned no data")
  if len(data) > _MAX_DOWNLOAD_BYTES:
    raise ArtError(f"{url} is larger than {_MAX_DOWNLOAD_BYTES} bytes")
  return data


@dataclasses.dataclass(frozen=True)
class ArtStore:
  """A content-addressed store of cover images on disk.

  Attributes:
    directory: Where images are kept.
  """
  directory: pathlib.Path = DEFAULT_STORE_DIR

  def path_for(self, art_hash: str) -> pathlib.Path:
    """Returns where an image with a given hash lives.

    Args:
      art_hash: The image's SHA-256, as hex.

    Returns:
      The path, whether or not the file exists.
    """
    return self.directory / f"{art_hash}.jpg"

  def has(self, art_hash: str) -> bool:
    """Returns whether an image is already stored.

    Args:
      art_hash: The image's SHA-256, as hex.

    Returns:
      True if it is present.
    """
    return self.path_for(art_hash).is_file()

  def put(self, data: bytes) -> str:
    """Stores image bytes, returning their content address.

    Storing the same image twice is free: the second call finds the file
    already there and writes nothing.

    Args:
      data: The image bytes, already normalised.

    Returns:
      The stored image's hash.

    Raises:
      ArtError: If the file cannot be written.
    """
    art_hash = digest(data)
    destination = self.path_for(art_hash)
    if destination.is_file():
      return art_hash
    try:
      self.directory.mkdir(parents=True, exist_ok=True)
      partial = destination.with_suffix(".part")
      partial.write_bytes(data)
      partial.replace(destination)
    except OSError as err:
      raise ArtError(f"could not store album art: {err}") from err
    return art_hash

  def get(self, art_hash: str) -> bytes:
    """Reads a stored image.

    Args:
      art_hash: The image's SHA-256, as hex.

    Returns:
      The image bytes.

    Raises:
      ArtError: If the image is not in the store.
    """
    try:
      return self.path_for(art_hash).read_bytes()
    except OSError as err:
      raise ArtError(f"album art {art_hash[:12]} is not in the store: "
                     f"{err}") from err

  def store_url(self, url: str) -> str:
    """Fetches, normalises and stores an image from a URL.

    Args:
      url: Where to fetch the image from.

    Returns:
      The stored image's hash.

    Raises:
      ArtError: If any step fails.
    """
    return self.put(normalise(fetch(url)))
