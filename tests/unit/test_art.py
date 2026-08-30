"""Tests for cover art normalisation and content-addressed storage."""

import io
import pathlib

import pytest
from PIL import Image

from music_match.tagging import art


def image_bytes(size: tuple[int, int] = (300, 200),
                colour: tuple[int, int, int] = (20, 90, 160),
                fmt: str = "PNG") -> bytes:
  """Builds an image to work with.

  Args:
    size: Width and height.
    colour: Fill colour.
    fmt: The format to encode as.

  Returns:
    The encoded image bytes.
  """
  buffer = io.BytesIO()
  Image.new("RGB", size, colour).save(buffer, fmt)
  return buffer.getvalue()


def test_normalise_produces_the_embedded_size() -> None:
  """640x640 JPEG is what Rekordbox wants and what gets embedded."""
  normalised = art.normalise(image_bytes())
  with Image.open(io.BytesIO(normalised)) as opened:
    assert opened.size == (art.TARGET_SIZE, art.TARGET_SIZE)
    assert opened.format == "JPEG"


def test_normalise_accepts_any_readable_format() -> None:
  """Sources serve PNG and JPEG interchangeably."""
  assert art.normalise(image_bytes(fmt="PNG"))
  assert art.normalise(image_bytes(fmt="JPEG"))


def test_normalise_rejects_non_images() -> None:
  """A redirect to an error page must not end up embedded in a file."""
  with pytest.raises(art.ArtError, match="could not read image"):
    art.normalise(b"<html>not an image</html>")


def test_digest_is_content_addressed() -> None:
  """The same bytes always give the same address, different bytes not."""
  first = art.normalise(image_bytes(colour=(1, 2, 3)))
  second = art.normalise(image_bytes(colour=(200, 100, 50)))
  assert art.digest(first) == art.digest(first)
  assert art.digest(first) != art.digest(second)


def test_store_round_trips(tmp_path: pathlib.Path) -> None:
  """What goes in comes back byte-identical."""
  store = art.ArtStore(tmp_path)
  data = art.normalise(image_bytes())
  stored = store.put(data)
  assert store.has(stored)
  assert store.get(stored) == data


def test_store_deduplicates(tmp_path: pathlib.Path) -> None:
  """Every track on an album embeds the same cover; store it once.

  This is the reason for content addressing rather than a file per
  track.
  """
  store = art.ArtStore(tmp_path)
  data = art.normalise(image_bytes())
  first = store.put(data)
  second = store.put(data)
  assert first == second
  assert len(list(tmp_path.glob("*.jpg"))) == 1


def test_store_leaves_no_partial_file(tmp_path: pathlib.Path) -> None:
  """Writes land under a temporary name and are renamed on success."""
  store = art.ArtStore(tmp_path)
  store.put(art.normalise(image_bytes()))
  assert not list(tmp_path.glob("*.part"))


def test_missing_art_is_an_error(tmp_path: pathlib.Path) -> None:
  """Asking for a hash that was never stored says so clearly."""
  with pytest.raises(art.ArtError, match="not in the store"):
    art.ArtStore(tmp_path).get("0" * 64)


def test_store_creates_its_directory(tmp_path: pathlib.Path) -> None:
  """A first run has no store directory yet."""
  store = art.ArtStore(tmp_path / "nested" / "art-store")
  stored = store.put(art.normalise(image_bytes()))
  assert store.has(stored)
