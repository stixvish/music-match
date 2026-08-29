"""Tag reading and writing, fingerprinting, genre detection, matching."""

from music_match.tagging.fields import NUMERIC_FIELDS
from music_match.tagging.fields import TEXT_FIELDS
from music_match.tagging.fields import TrackTags
from music_match.tagging.tags import TagError
from music_match.tagging.tags import get_tags
from music_match.tagging.tags import read_tags
from music_match.tagging.tags import write_tags

__all__ = [
    "NUMERIC_FIELDS",
    "TEXT_FIELDS",
    "TagError",
    "TrackTags",
    "get_tags",
    "read_tags",
    "write_tags",
]
