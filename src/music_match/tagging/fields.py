"""The set of metadata fields this tool cares about.

Most are Rekordbox-visible. `isrc` and `genre` are not shown by Rekordbox
but are required for matching and for the library to be useful elsewhere.
`source_video_id` is internal bookkeeping — written to a non-Rekordbox
field at download time so `reindex` can rebuild the download-archive log
from the files alone.
"""

import dataclasses
from typing import Any, Mapping

TEXT_FIELDS = (
    "title",
    "artist",
    "original_artist",
    "composer",
    "lyricist",
    "remixer",
    "mix_name",
    "album",
    "album_artist",
    "genre",
    "isrc",
    "release_date",
    "source_video_id",
)

NUMERIC_FIELDS = (
    "year",
    "track_number",
    "track_total",
    "disc_number",
    "disc_total",
)

ALL_FIELDS = TEXT_FIELDS + NUMERIC_FIELDS

# What a file needs before it is worth leaving alone. Not every target
# field — a track can be perfectly usable without a lyricist — but enough
# that re-matching it would be work for nothing.
REQUIRED_FIELDS = ("title", "artist", "album", "year")


@dataclasses.dataclass(frozen=True)
class TrackTags:
  """A track's metadata, format-independent.

  Every field is optional. `None` means "not known" — never "known to be
  empty" — which is what lets a partial result from one source be merged
  with a partial result from another.
  """
  title: str | None = None
  artist: str | None = None
  original_artist: str | None = None
  composer: str | None = None
  lyricist: str | None = None
  remixer: str | None = None
  mix_name: str | None = None
  album: str | None = None
  album_artist: str | None = None
  genre: str | None = None
  isrc: str | None = None
  release_date: str | None = None
  source_video_id: str | None = None
  year: int | None = None
  track_number: int | None = None
  track_total: int | None = None
  disc_number: int | None = None
  disc_total: int | None = None

  def as_dict(self, *, include_empty: bool = False) -> dict[str, Any]:
    """Returns the tags as a plain dict.

    Args:
      include_empty: Whether to include fields that are None.

    Returns:
      Field name to value, in the order fields are declared.
    """
    raw = dataclasses.asdict(self)
    if include_empty:
      return raw
    return {key: value for key, value in raw.items() if value is not None}

  def is_empty(self) -> bool:
    """Returns whether every field is unset."""
    return not self.as_dict()

  def is_complete(self) -> bool:
    """Returns whether this file already carries usable metadata.

    Used by `reindex` to tell a library that merely lost its database
    from one that was never tagged. Deliberately not every target field:
    a track is fine without a lyricist, and demanding one would send the
    whole library back through matching.
    """
    present = self.as_dict()
    return all(field in present for field in REQUIRED_FIELDS)

  def missing_required(self) -> tuple[str, ...]:
    """Returns the required fields this file does not have.

    Returns:
      The missing field names, in declaration order.
    """
    present = self.as_dict()
    return tuple(field for field in REQUIRED_FIELDS if field not in present)

  def merged_with(self, other: "TrackTags") -> "TrackTags":
    """Fills this object's unset fields from another.

    Values already set here win; `other` only supplies what is missing.
    This is the merge direction source matching needs — a higher-precedence
    source is merged first, and lower-precedence ones fill the gaps.

    Args:
      other: Tags to draw missing values from.

    Returns:
      A new TrackTags. Neither input is modified.
    """
    return dataclasses.replace(self, **other.as_dict() | self.as_dict())

  def changes_against(self, current: "TrackTags") -> dict[str, tuple[Any, Any]]:
    """Finds which fields this object would change on an existing track.

    Fields unset here are left alone rather than cleared, so they never
    appear as a change.

    Args:
      current: The track's existing tags.

    Returns:
      Field name to (old value, new value) for every field that differs.
      Empty if writing this object would be a no-op.
    """
    changes: dict[str, tuple[Any, Any]] = {}
    existing = current.as_dict(include_empty=True)
    for field, new_value in self.as_dict().items():
      old_value = existing[field]
      if old_value != new_value:
        changes[field] = (old_value, new_value)
    return changes

  @classmethod
  def from_mapping(cls, values: Mapping[str, Any]) -> "TrackTags":
    """Builds tags from a mapping, ignoring keys that are not fields.

    Args:
      values: Field name to value. Unknown keys are dropped.

    Returns:
      The constructed TrackTags.
    """
    known = {key: values[key] for key in ALL_FIELDS if key in values}
    return cls(**known)
