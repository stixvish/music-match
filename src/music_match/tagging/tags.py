"""Reading and writing file tags through mutagen.

Two container formats matter here: MP4 atoms (the `~1990 M4A files from
yt-dlp) and ID3 frames (MP3, AIFF, and the ID3-in-WAV chunk). Everything
above this module works in terms of `TrackTags` and never sees an atom
name or a frame ID.

Note on typing: `mutagen.FileType.tags` is `Optional[Tags]` and is not
narrowed per subclass, so `isinstance(audio, MP3)` does not tell mypy the
tags exist. Always go through `get_tags`, which asserts explicitly.
"""

import pathlib
from typing import Any, cast

import mutagen
from mutagen import id3
from mutagen import mp4

from music_match.tagging.fields import TrackTags

# Internal bookkeeping namespace, deliberately outside the fields any
# player (Rekordbox included) displays.
_FREEFORM_NAMESPACE = "com.apple.iTunes"
_INTERNAL_NAMESPACE = "com.music-match"
_SOURCE_ID_DESC = "SOURCE_VIDEO_ID"

# Simple one-to-one text mappings. Numbers and dates need more than a
# name lookup and are handled separately below.
_ID3_TEXT_FRAMES = {
    "title": "TIT2",
    "artist": "TPE1",
    "original_artist": "TOPE",
    "composer": "TCOM",
    "lyricist": "TEXT",
    "remixer": "TPE4",
    "mix_name": "TIT3",
    "album": "TALB",
    "album_artist": "TPE2",
    "genre": "TCON",
    "isrc": "TSRC",
}

_MP4_TEXT_ATOMS = {
    "title": "\xa9nam",
    "artist": "\xa9ART",
    "album": "\xa9alb",
    "album_artist": "aART",
    "composer": "\xa9wrt",
    "genre": "\xa9gen",
}

# MP4 has no standard atom for these, so they go in iTunes freeform
# atoms under the well-known namespace other taggers use.
_MP4_FREEFORM = {
    "original_artist": "ORIGINAL ARTIST",
    "lyricist": "LYRICIST",
    "remixer": "REMIXER",
    "mix_name": "MIXNAME",
    "isrc": "ISRC",
}


class TagError(Exception):
  """Raised when a file cannot be opened or its tag format is unsupported."""


def get_tags(audio: mutagen.FileType) -> mutagen.Tags:
  """Returns audio.tags, creating an empty tag container if absent.

  Args:
    audio: A loaded mutagen file object.

  Returns:
    The file's tag container, guaranteed non-None.
  """
  if audio.tags is None:
    audio.add_tags()
  assert audio.tags is not None
  return audio.tags


def load_audio(path: pathlib.Path) -> mutagen.FileType:
  """Opens an audio file with mutagen.

  Args:
    path: Path to the audio file.

  Returns:
    The loaded file object.

  Raises:
    TagError: If the file is missing, unreadable, or not a format mutagen
      recognizes.
  """
  try:
    audio = cast(mutagen.FileType | None, mutagen.File(path))
  except (OSError, mutagen.MutagenError) as err:
    raise TagError(f"could not read {path}: {err}") from err
  if audio is None:
    raise TagError(f"unrecognized audio format: {path}")
  return audio


def read_tags(path: pathlib.Path) -> TrackTags:
  """Reads a file's metadata into a format-independent TrackTags.

  Args:
    path: Path to the audio file.

  Returns:
    The tags present on the file. Absent fields are None.

  Raises:
    TagError: If the file cannot be read or its tag format is unsupported.
  """
  container = get_tags(load_audio(path))
  if isinstance(container, id3.ID3):
    return _read_id3(container)
  if isinstance(container, mp4.MP4Tags):
    return _read_mp4(container)
  raise TagError(
      f"unsupported tag container {type(container).__name__}: {path}")


def write_tags(path: pathlib.Path,
               tags: TrackTags,
               *,
               dry_run: bool = False) -> dict[str, tuple[Any, Any]]:
  """Writes metadata to a file, leaving unset fields untouched.

  Only fields set on `tags` are written. A field left as None means "no
  opinion" and never clears an existing value — clearing is an explicit
  decision that belongs to the caller, not to this wrapper.

  Args:
    path: Path to the audio file.
    tags: The values to write.
    dry_run: If True, compute the changes but do not touch the file.

  Returns:
    Field name to (old value, new value) for everything that changed, or
    would have changed under dry_run. Empty if the write was a no-op.

  Raises:
    TagError: If the file cannot be read or written, or its tag format is
      unsupported.
  """
  audio = load_audio(path)
  container = get_tags(audio)
  if isinstance(container, id3.ID3):
    current = _read_id3(container)
  elif isinstance(container, mp4.MP4Tags):
    current = _read_mp4(container)
  else:
    raise TagError(
        f"unsupported tag container {type(container).__name__}: {path}")

  changes = tags.changes_against(current)
  if dry_run or not changes:
    return changes

  merged = tags.merged_with(current)
  if isinstance(container, id3.ID3):
    _write_id3(container, merged)
  else:
    _write_mp4(container, merged)
  try:
    audio.save()
  except (OSError, mutagen.MutagenError) as err:
    raise TagError(f"could not write {path}: {err}") from err
  return changes


def _first_text(container: id3.ID3, frame_id: str) -> str | None:
  """Returns the first text value of an ID3 frame, or None if absent.

  Args:
    container: The file's ID3 tags.
    frame_id: A frame ID such as "TIT2".

  Returns:
    The first value as a string, or None if the frame is missing or empty.
  """
  frames = container.getall(frame_id)
  if not frames:
    return None
  values = [str(value) for value in frames[0].text if str(value).strip()]
  return values[0] if values else None


def _split_pair(value: str | None) -> tuple[int | None, int | None]:
  """Parses an ID3 "number/total" string.

  Args:
    value: Text such as "3/12", "3", or None.

  Returns:
    (number, total), either of which may be None if absent or unparsable.
  """
  if not value:
    return (None, None)
  number, _, total = value.partition("/")
  return (_as_int(number), _as_int(total))


def _as_int(value: str) -> int | None:
  """Parses an integer, returning None rather than raising.

  Args:
    value: The text to parse.

  Returns:
    The integer, or None if the text is not a plain integer.
  """
  try:
    return int(value.strip())
  except ValueError:
    return None


def _year_from_date(value: str | None) -> int | None:
  """Extracts a four-digit year from the front of a date string.

  Args:
    value: A date such as "2019-05-03", "2019", or None.

  Returns:
    The year, or None if the string does not start with four digits.
  """
  if not value or len(value) < 4 or not value[:4].isdigit():
    return None
  return int(value[:4])


def _read_id3(container: id3.ID3) -> TrackTags:
  """Reads ID3 frames into TrackTags.

  Args:
    container: The file's ID3 tags.

  Returns:
    The tags present on the file.
  """
  values: dict[str, Any] = {
      field: _first_text(container, frame_id)
      for field, frame_id in _ID3_TEXT_FRAMES.items()
  }

  recorded = _first_text(container, "TDRC")
  released = _first_text(container, "TDRL")
  values["release_date"] = released or (recorded
                                        if _is_full_date(recorded) else None)
  values["year"] = _year_from_date(recorded or released)

  track_number, track_total = _split_pair(_first_text(container, "TRCK"))
  disc_number, disc_total = _split_pair(_first_text(container, "TPOS"))
  values.update(
      track_number=track_number,
      track_total=track_total,
      disc_number=disc_number,
      disc_total=disc_total,
      source_video_id=_read_id3_source_id(container),
  )
  return TrackTags.from_mapping(values)


def _is_full_date(value: str | None) -> bool:
  """Returns whether a date string carries more than just a year.

  Args:
    value: A date string, or None.

  Returns:
    True if the value looks like "YYYY-MM" or longer.
  """
  return bool(value) and len(cast(str, value)) > 4


def _read_id3_source_id(container: id3.ID3) -> str | None:
  """Reads the internal source video ID from its TXXX frame.

  Args:
    container: The file's ID3 tags.

  Returns:
    The stored video ID, or None if the file has none.
  """
  for frame in container.getall("TXXX"):
    if frame.desc == _SOURCE_ID_DESC and frame.text:
      return str(frame.text[0])
  return None


def _write_id3(container: id3.ID3, tags: TrackTags) -> None:
  """Writes TrackTags onto an ID3 container in place.

  Args:
    container: The file's ID3 tags, modified in place.
    tags: The values to write. Unset fields are left untouched.
  """
  values = tags.as_dict()
  for field, frame_id in _ID3_TEXT_FRAMES.items():
    if field in values:
      frame_class = getattr(id3, frame_id)
      container.setall(frame_id,
                       [frame_class(encoding=3, text=[values[field]])])

  # TDRC is what players (Rekordbox included) read for the year, so it
  # carries the most precise date known; TDRL additionally records that
  # the date is a release date when we know it is.
  date = values.get("release_date") or _year_text(values.get("year"))
  if date is not None:
    container.setall("TDRC", [id3.TDRC(encoding=3, text=[date])])
  if "release_date" in values:
    released = values["release_date"]
    container.setall("TDRL", [id3.TDRL(encoding=3, text=[released])])

  for frame_id, number, total in (
      ("TRCK", values.get("track_number"), values.get("track_total")),
      ("TPOS", values.get("disc_number"), values.get("disc_total")),
  ):
    text = _pair_text(number, total)
    if text is not None:
      frame_class = getattr(id3, frame_id)
      container.setall(frame_id, [frame_class(encoding=3, text=[text])])

  if "source_video_id" in values:
    others = [
        frame for frame in container.getall("TXXX")
        if frame.desc != _SOURCE_ID_DESC
    ]
    container.setall(
        "TXXX", others + [
            id3.TXXX(encoding=3,
                     desc=_SOURCE_ID_DESC,
                     text=[values["source_video_id"]])
        ])


def _year_text(year: int | None) -> str | None:
  """Formats a year for a date frame.

  Args:
    year: The year, or None.

  Returns:
    The year as a string, or None.
  """
  return None if year is None else str(year)


def _pair_text(number: int | None, total: int | None) -> str | None:
  """Formats a number/total pair for ID3.

  Args:
    number: The track or disc number.
    total: The total count, if known.

  Returns:
    "n/total", "n", or None when there is no number to write.
  """
  if number is None:
    return None
  return f"{number}/{total}" if total is not None else str(number)


def _read_mp4(container: mp4.MP4Tags) -> TrackTags:
  """Reads MP4 atoms into TrackTags.

  Args:
    container: The file's MP4 tags.

  Returns:
    The tags present on the file.
  """
  values: dict[str, Any] = {}
  for field, atom in _MP4_TEXT_ATOMS.items():
    raw = container.get(atom)
    if raw:
      values[field] = str(raw[0])
  for field, name in _MP4_FREEFORM.items():
    values[field] = _read_freeform(container, _FREEFORM_NAMESPACE, name)
  values["source_video_id"] = _read_freeform(container, _INTERNAL_NAMESPACE,
                                             _SOURCE_ID_DESC)

  date = container.get("\xa9day")
  date_text = str(date[0]) if date else None
  values["release_date"] = date_text if _is_full_date(date_text) else None
  values["year"] = _year_from_date(date_text)

  number, total = _read_mp4_pair(container, "trkn")
  values.update(track_number=number, track_total=total)
  number, total = _read_mp4_pair(container, "disk")
  values.update(disc_number=number, disc_total=total)
  return TrackTags.from_mapping(values)


def _read_mp4_pair(container: mp4.MP4Tags,
                   atom: str) -> tuple[int | None, int | None]:
  """Reads an MP4 (number, total) atom such as `trkn` or `disk`.

  Args:
    container: The file's MP4 tags.
    atom: The atom name.

  Returns:
    (number, total). Either may be None; MP4 stores 0 to mean "unset".
  """
  raw = container.get(atom)
  if not raw:
    return (None, None)
  pair = tuple(raw[0])
  number = pair[0] if len(pair) > 0 and pair[0] else None
  total = pair[1] if len(pair) > 1 and pair[1] else None
  return (number, total)


def _freeform_atom(namespace: str, name: str) -> str:
  """Builds an MP4 freeform atom key.

  Args:
    namespace: The reverse-DNS namespace, e.g. "com.apple.iTunes".
    name: The field name within that namespace.

  Returns:
    The atom key mutagen uses for that freeform field.
  """
  return f"----:{namespace}:{name}"


def _read_freeform(container: mp4.MP4Tags, namespace: str,
                   name: str) -> str | None:
  """Reads a freeform MP4 atom as text.

  Args:
    container: The file's MP4 tags.
    namespace: The reverse-DNS namespace the field lives under.
    name: The field name within that namespace.

  Returns:
    The decoded value, or None if the atom is absent or not valid UTF-8.
  """
  raw = container.get(_freeform_atom(namespace, name))
  if not raw:
    return None
  try:
    return bytes(raw[0]).decode("utf-8")
  except UnicodeDecodeError:
    return None


def _write_mp4(container: mp4.MP4Tags, tags: TrackTags) -> None:
  """Writes TrackTags onto an MP4 container in place.

  Args:
    container: The file's MP4 tags, modified in place.
    tags: The values to write. Unset fields are left untouched.
  """
  values = tags.as_dict()
  for field, atom in _MP4_TEXT_ATOMS.items():
    if field in values:
      container[atom] = [values[field]]
  for field, name in _MP4_FREEFORM.items():
    if field in values:
      _write_freeform(container, _FREEFORM_NAMESPACE, name, values[field])
  if "source_video_id" in values:
    _write_freeform(container, _INTERNAL_NAMESPACE, _SOURCE_ID_DESC,
                    values["source_video_id"])

  date = values.get("release_date") or _year_text(values.get("year"))
  if date is not None:
    container["\xa9day"] = [date]

  for atom, number, total in (
      ("trkn", values.get("track_number"), values.get("track_total")),
      ("disk", values.get("disc_number"), values.get("disc_total")),
  ):
    if number is not None:
      container[atom] = [(number, total or 0)]


def _write_freeform(container: mp4.MP4Tags, namespace: str, name: str,
                    value: str) -> None:
  """Writes a text value into a freeform MP4 atom.

  Args:
    container: The file's MP4 tags, modified in place.
    namespace: The reverse-DNS namespace to write under.
    name: The field name within that namespace.
    value: The text to store.
  """
  container[_freeform_atom(namespace, name)] = [
      mp4.MP4FreeForm(value.encode("utf-8"), dataformat=mp4.AtomDataType.UTF8)
  ]
