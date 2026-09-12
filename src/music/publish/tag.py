"""Write ID3v2.4 frames into AIFF.

Every frame below was confirmed by importing probe files into rekordbox 7 and
serato dj lite and reading the values off the screen (SPEC.md §10). Do not
change this map without re-running that probe.
"""

from dataclasses import dataclass, field
from pathlib import Path

from mutagen.aiff import AIFF
from mutagen.id3 import APIC, COMM, Frames

# field name -> id3v2.4 frame id. confirmed, not assumed (SPEC.md §10).
FRAME_MAP: dict[str, str] = {
  "title": "TIT2",
  "artist": "TPE1",
  "album": "TALB",
  "album_artist": "TPE2",
  "genre": "TCON",
  "track_number": "TRCK",
  "disc_number": "TPOS",
  "bpm": "TBPM",
  "composer": "TCOM",
  "lyricist": "TEXT",
  "remixer": "TPE4",
  "mix_name": "TIT3",
  "label": "TPUB",
  "original_artist": "TOPE",
  "key": "TKEY",
  "isrc": "TSRC",
  "grouping": "TIT1",
  # rekordbox shows TDRC as Year and TDRL as Release Date, in separate
  # columns — so both are written.
  "year": "TDRC",
  "release_date": "TDRL",
}


# frames this project writes. everything else belongs to another tool and is
# preserved untouched (SPEC.md §10).
OWNED_FRAMES = frozenset(FRAME_MAP.values()) | {"COMM", "APIC"}


@dataclass
class Tags:
  """A complete tag set, keyed by the names in FRAME_MAP."""

  values: dict[str, str] = field(default_factory=dict)
  comment: str | None = None
  artwork: bytes | None = None

  def __setitem__(self, key: str, value: str) -> None:
    """Set one field, rejecting names with no known frame."""
    if key not in FRAME_MAP:
      raise KeyError(f"no id3 frame mapped for {key!r}")
    self.values[key] = value

  def __getitem__(self, key: str) -> str:
    """Read one field."""
    return self.values[key]

  def __contains__(self, key: str) -> bool:
    """Whether a field has been set."""
    return key in self.values


def write(path: Path, tags: Tags) -> None:
  """Write tags into an AIFF file, replacing whatever was there.

  Args:
    path: AIFF file to tag.
    tags: Values to write. Unset fields are omitted, not blanked.

  Raises:
    KeyError: If a field has no mapped frame.
  """
  audio = AIFF(str(path))
  if audio.tags is None:
    audio.add_tags()
  assert audio.tags is not None

  # Frames this project does not own are preserved across a rewrite. Serato
  # stores its beatgrid and cue points in GEOB frames inside the ID3 tag, and
  # a naive delete-then-write destroys them — verified on real files that
  # already carried Serato BeatGrid, Markers2, Autotags and Overview.
  foreign = [
    frame for key, frame in audio.tags.items() if key.split(":")[0] not in OWNED_FRAMES
  ]
  audio.tags.delete(str(path))
  for frame in foreign:
    audio.tags.add(frame)

  for name, value in tags.values.items():
    if value in (None, ""):
      continue
    frame_id = FRAME_MAP[name]
    frame_cls = Frames.get(frame_id)
    if frame_cls is None:  # pragma: no cover - FRAME_MAP is fixed and valid
      raise KeyError(f"mutagen has no frame class for {frame_id}")
    audio.tags.add(frame_cls(encoding=3, text=[str(value)]))

  if tags.comment:
    # COMM renders in both rekordbox and serato (SPEC.md §10).
    audio.tags.add(COMM(encoding=3, lang="eng", desc="", text=[tags.comment]))
  if tags.artwork:
    audio.tags.add(
      APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=tags.artwork)
    )
  audio.save(v2_version=4)


def read(path: Path) -> Tags:
  """Read tags back out, for verification and round-trip tests.

  Args:
    path: AIFF file.

  Returns:
    The tags found.
  """
  audio = AIFF(str(path))
  result = Tags()
  if audio.tags is None:
    return result
  reverse = {v: k for k, v in FRAME_MAP.items()}
  for frame_id, frame in audio.tags.items():
    base = frame_id.split(":")[0]
    if base in reverse:
      result.values[reverse[base]] = str(frame.text[0])
    elif base == "COMM":
      result.comment = str(frame.text[0])
    elif base == "APIC":
      result.artwork = frame.data
  return result
