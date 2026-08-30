"""Spotting audio that was taken from a music video.

A rip from a music video is not the album track: it often carries an
intro, dialogue, applause or an outro, and sometimes a different mix
entirely. Matching one against a metadata source wastes the API call and
tends to produce a confident match to a recording the file does not
actually contain.

Detection is on the filename and the embedded title only — deliberately
cheap, and run before anything else in the pipeline. Anything flagged is
quarantined for a human to confirm rather than acted on, so the cost of a
false positive is a look rather than a wrong tag.

The marker list is drawn from what this library actually contains: of
2412 files, 118 carry one, and every file whose name mentions "video" at
all matches one of these phrases — there was no case of an ordinary title
being caught by accident.
"""

import dataclasses
import pathlib
import re

# Phrases meaning the audio came from a video. "vevo" is included
# because that platform hosts nothing else, and it appears in the
# uploader half of a yt-dlp filename where the title half may not carry a
# marker of its own.
VIDEO_MARKERS = (
    "official music video",
    "official video",
    "music video",
    "video oficial",
    "videoclip",
    "official performance video",
    "live performance",
    "vevo",
)

# Markers that attach to the end of a word rather than standing alone.
# yt-dlp writes the uploader into the filename, and a VEVO channel is
# named "DJJazzyJeffVEVO" — requiring a word boundary in front means the
# marker never matches the form it actually takes.
SUFFIX_MARKERS = ("vevo",)

# Phrases that look like markers but are not. A lyric video or a
# visualiser carries the studio audio — the picture is decoration, and
# the file is exactly what it should be. Flagging these would put a
# tenth of the library in a review queue for nothing.
AUDIO_ONLY_MARKERS = (
    "official audio",
    "official lyric video",
    "lyric video",
    "lyrics",
    "official visualizer",
    "official visualiser",
    "visualizer",
    "visualiser",
    "audio only",
)


@dataclasses.dataclass(frozen=True)
class Detection:
  """Whether a file looks like a video rip, and why.

  Attributes:
    is_rip: Whether it should be quarantined.
    markers: The phrases found, in the order listed above.
    where: Where each marker was found — "filename" or "title".
  """
  is_rip: bool
  markers: tuple[str, ...] = ()
  where: tuple[str, ...] = ()

  def describe(self) -> str:
    """Returns a short explanation for CLI output.

    Returns:
      The markers and where they were seen.
    """
    if not self.markers:
      return "no video markers"
    found = ", ".join(f"{marker} ({place})"
                      for marker, place in zip(self.markers, self.where))
    return found


def flatten(text: str) -> str:
  """Lowercases text and reduces punctuation to single spaces.

  Deliberately *not* `matching.normalize`, which strips exactly these
  phrases as upload noise — it exists to make "Cardi B - Bodak Yellow
  OFFICIAL MUSIC VIDEO" compare equal to "Bodak Yellow". Running
  detection through it removes every marker before the search begins.
  Matching and detection want opposite things from a normaliser.

  Args:
    text: The raw filename stem or embedded title.

  Returns:
    Lowercase words separated by single spaces.
  """
  lowered = text.replace("_", " ").lower()
  return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def _contains(haystack: str, phrase: str) -> bool:
  """Returns whether a normalised string contains a phrase as whole words.

  Args:
    haystack: The normalised text to search.
    phrase: The phrase to look for.

  Returns:
    True if present on word boundaries, so "vevo" does not match inside
    a longer word.
  """
  leading = "" if phrase in SUFFIX_MARKERS else r"\b"
  return re.search(rf"{leading}{re.escape(phrase)}\b", haystack) is not None


def markers_in(text: str | None) -> tuple[str, ...]:
  """Finds the video markers in one piece of text.

  Audio-only phrases are removed first, so "Official Lyric Video" is not
  read as the "video" half of a rip marker.

  Args:
    text: A filename stem or an embedded title.

  Returns:
    The video markers present, in declaration order.
  """
  if not text:
    return ()
  normalised = flatten(text)
  for phrase in AUDIO_ONLY_MARKERS:
    normalised = normalised.replace(phrase, " ")
  found = [marker for marker in VIDEO_MARKERS if _contains(normalised, marker)]
  # "official music video" contains "music video"; reporting both says
  # the same thing twice.
  return tuple(
      marker for marker in found
      if not any(marker != other and marker in other for other in found))


def detect(path: pathlib.Path, title: str | None = None) -> Detection:
  """Decides whether a file looks like a rip from a music video.

  Args:
    path: The audio file, whose name is checked.
    title: The file's embedded title, if it has one.

  Returns:
    The detection, naming every marker found and where.
  """
  markers: list[str] = []
  places: list[str] = []
  for place, text in (("filename", path.stem), ("title", title)):
    for marker in markers_in(text):
      if marker not in markers:
        markers.append(marker)
        places.append(place)
  return Detection(is_rip=bool(markers),
                   markers=tuple(markers),
                   where=tuple(places))
