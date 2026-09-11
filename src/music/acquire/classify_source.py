"""Tell a clean album track from a music-video rip (SPEC.md §8).

A music video carries intro skits, crowd noise and outros that are not part of
the song. 117 files in the existing library are rips of this kind. The signals
below are pure functions over metadata so they run in CI — no audio, no network.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

# label-provided "art tracks" are clean album audio.
TOPIC_SUFFIX = " - Topic"
PROVIDED_BY = "provided to youtube by"

_VIDEO_WORDS = re.compile(
  r"\b(?:official\s*(?:music\s*)?video|music\s*video|lyric\s*video|"
  r"visuali[sz]er|m/v|live\s+(?:at|from|in)|performance|behind\s+the\s+scenes)\b",
  re.IGNORECASE,
)
# beyond this, the extra runtime is not the song
DURATION_TOLERANCE_S = 5.0


class Verdict(StrEnum):
  """What kind of source a download came from."""

  ART_TRACK = "art_track"
  VIDEO_RIP = "video_rip"
  UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceSignals:
  """Everything the classifier looks at."""

  channel: str = ""
  title: str = ""
  description: str = ""
  artist_tag: str = ""
  duration_s: float | None = None
  catalog_duration_s: float | None = None

  @property
  def duration_delta_s(self) -> float | None:
    """How much longer this file is than the catalogue says it should be."""
    if self.duration_s is None or self.catalog_duration_s is None:
      return None
    return self.duration_s - self.catalog_duration_s


@dataclass(frozen=True)
class Classification:
  """A verdict plus the signals that produced it."""

  verdict: Verdict
  reasons: tuple[str, ...] = ()

  @property
  def is_video_rip(self) -> bool:
    """Whether this should be flagged or re-fetched as an art track."""
    return self.verdict is Verdict.VIDEO_RIP


def classify(signals: SourceSignals) -> Classification:
  """Decide whether a download is clean album audio or a video rip.

  Positive art-track evidence wins outright: a Topic channel or a
  "Provided to YouTube by" description means the label delivered the audio, and
  no amount of title noise changes that.

  Args:
    signals: Metadata from the download.

  Returns:
    The verdict and the reasons behind it.
  """
  positives: list[str] = []
  if signals.channel.endswith(TOPIC_SUFFIX):
    positives.append("topic_channel")
  if PROVIDED_BY in signals.description.casefold():
    positives.append("provided_to_youtube_by")
  if positives:
    return Classification(Verdict.ART_TRACK, tuple(positives))

  # strong signals convict on their own.
  strong: list[str] = []
  if _VIDEO_WORDS.search(signals.title):
    strong.append("video_in_title")
  delta = signals.duration_delta_s
  if delta is not None and delta > DURATION_TOLERANCE_S:
    strong.append("longer_than_catalogue")

  # weak signals are recorded but never decide alone. an artist tag equal to
  # the channel name describes an official artist-channel upload just as well
  # as a rip — on its own it flagged all 2,329 library files.
  weak: list[str] = []
  if (
    signals.artist_tag
    and signals.channel
    and signals.artist_tag.casefold().replace("_", " ")
    == signals.channel.casefold().replace("_", " ")
  ):
    weak.append("artist_is_channel")

  if strong:
    return Classification(Verdict.VIDEO_RIP, tuple(strong + weak))
  return Classification(Verdict.UNKNOWN, tuple(weak))


def art_track_query(artist: str, title: str) -> str:
  """Build a YouTube Music search that prefers label-delivered audio.

  Preferring art tracks at *search* time avoids the problem rather than
  detecting it afterwards (SPEC.md §8).

  Args:
    artist: Normalised artist.
    title: Normalised title.

  Returns:
    A search query string.
  """
  return f"{artist} {title}".strip()
