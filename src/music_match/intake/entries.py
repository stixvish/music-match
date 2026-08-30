"""Turning submitted links into individual downloadable entries.

A submission is a mix of single tracks, albums and playlists. Expanding
them is metadata-only — yt-dlp is asked for the listing, never the audio
— so a large paste costs one request per link rather than a download per
track, and the dedup layers get to run before anything is fetched.
"""

import dataclasses
from typing import Any, Iterable

import yt_dlp

# Enough to list a playlist's contents without resolving each entry.
_FLAT_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
}


class IntakeError(Exception):
  """Raised when a link cannot be read."""


@dataclasses.dataclass(frozen=True)
class Entry:
  """One downloadable track, as the source describes it.

  Attributes:
    video_id: The source's own identifier, which is what the archive is
      keyed on.
    extractor: Which site it came from, lowercased. Paired with
      `video_id` because two sites may use the same id.
    title: The upload's title, if known.
    uploader: Who posted it, if known.
    duration_seconds: Its length, if known. The strongest signal the
      pre-check has.
    url: A URL that resolves to this entry alone.
  """
  video_id: str
  extractor: str
  title: str | None = None
  uploader: str | None = None
  duration_seconds: float | None = None
  url: str = ""

  def label(self) -> str:
    """Returns a short human-readable name for this entry."""
    if self.title and self.uploader:
      return f"{self.uploader} - {self.title}"
    return self.title or self.url or self.video_id


def parse_links(text: str) -> list[str]:
  """Reads submitted links from free text.

  Accepts one link per line, ignoring blank lines and `#` comments, so a
  file of links can be annotated.

  Args:
    text: The submitted text.

  Returns:
    The links, in order, with duplicates removed.
  """
  seen: dict[str, None] = {}
  for line in text.splitlines():
    stripped = line.split("#", 1)[0].strip()
    if stripped:
      seen[stripped] = None
  return list(seen)


def _as_entry(info: dict[str, Any], fallback_url: str = "") -> Entry | None:
  """Converts one yt-dlp info dict into an Entry.

  Args:
    info: A video info dict, flat or full.
    fallback_url: A URL to use when the dict carries none.

  Returns:
    The entry, or None if it has no usable identifier.
  """
  video_id = info.get("id")
  if not video_id:
    return None
  # A flat playlist entry names its extractor `ie_key`; a resolved video
  # names it `extractor`.
  extractor = info.get("extractor") or info.get("ie_key") or "unknown"
  duration = info.get("duration")
  return Entry(
      video_id=str(video_id),
      extractor=str(extractor).lower(),
      title=info.get("title"),
      uploader=info.get("uploader") or info.get("channel"),
      duration_seconds=float(duration) if duration else None,
      url=info.get("webpage_url") or info.get("url") or fallback_url,
  )


def entries_from_info(info: dict[str, Any], url: str = "") -> list[Entry]:
  """Flattens a yt-dlp result into individual entries.

  Args:
    info: What `extract_info` returned.
    url: The link it came from, used when an entry names no URL.

  Returns:
    One entry per track. A playlist yields its members; a single video
    yields itself.
  """
  if info.get("_type") == "playlist":
    found = []
    for item in info.get("entries") or []:
      if isinstance(item, dict):
        found.extend(entries_from_info(item, url))
    return found
  entry = _as_entry(info, url)
  return [entry] if entry is not None else []


def expand(urls: Iterable[str],
           *,
           options: dict[str, Any] | None = None) -> list[Entry]:
  """Expands submitted links into individual entries.

  A link that cannot be read is reported and skipped rather than ending
  the run: one dead URL in a pasted batch should not cost the rest.

  Args:
    urls: The submitted links.
    options: yt-dlp options to override the metadata-only defaults.

  Returns:
    Every entry found, in submission order, with duplicates removed.

  Raises:
    IntakeError: If every link failed.
  """
  settings = {**_FLAT_OPTIONS, **(options or {})}
  found: dict[tuple[str, str], Entry] = {}
  failures: list[str] = []
  with yt_dlp.YoutubeDL(settings) as downloader:
    for url in urls:
      try:
        info = downloader.extract_info(url, download=False)
      except yt_dlp.utils.DownloadError as err:
        failures.append(f"{url}: {err}")
        continue
      if not isinstance(info, dict):
        failures.append(f"{url}: no information returned")
        continue
      for entry in entries_from_info(info, url):
        found.setdefault((entry.extractor, entry.video_id), entry)

  if failures and not found:
    raise IntakeError("; ".join(failures))
  return list(found.values())
