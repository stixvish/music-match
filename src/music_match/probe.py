"""Comparing what every metadata source says about the same tracks.

This is the tool `precedence.toml` is tuned with. Rather than guessing
which source is best for which field, run real tracks through all of them
and look at what comes back — per field, side by side, and aggregated into
coverage counts across a whole sample.

It deliberately does no matching or scoring: every source's own top
candidate is reported as-is. Deciding which candidate is right, and how
confident to be, is the matcher's job in the next stage. Conflating the
two would mean tuning precedence against a matcher that precedence has not
been tuned for yet.
"""

import dataclasses
import pathlib
from typing import Mapping, Sequence

from music_match.sources.base import MetadataSource
from music_match.sources.base import SourceError
from music_match.sources.base import SourceQuery
from music_match.sources.base import SourceResult
from music_match.tagging import tags as tag_io
from music_match.tagging.fields import ALL_FIELDS

# Fields worth comparing across sources. `source_video_id` is internal
# bookkeeping that no external source can know about.
COMPARED_FIELDS = tuple(
    field for field in ALL_FIELDS if field != "source_video_id")


@dataclasses.dataclass(frozen=True)
class TrackProbe:
  """What every source said about one track.

  Attributes:
    query: What was asked.
    path: The file the query came from, if it came from one.
    results: Source name to its best candidate, or None if it had none.
    errors: Source name to why it could not answer, for sources that
      failed rather than returned nothing.
  """
  query: SourceQuery
  path: pathlib.Path | None
  results: Mapping[str, SourceResult | None]
  errors: Mapping[str, str]

  def value(self, source: str, field: str) -> str | None:
    """Returns one source's value for one field.

    Args:
      source: The source name.
      field: The field name.

    Returns:
      The value as text, or None if the source has no value for it.
    """
    result = self.results.get(source)
    if result is None:
      return None
    value = result.tags.as_dict().get(field)
    return None if value is None else str(value)

  def label(self) -> str:
    """Returns a human-readable name for this track."""
    if self.path is not None:
      return self.path.name
    return self.query.as_text() or "(unnamed)"


@dataclasses.dataclass(frozen=True)
class ProbeReport:
  """The result of probing a sample of tracks.

  Attributes:
    probes: One entry per track.
    source_names: The sources that were asked, in display order.
  """
  probes: tuple[TrackProbe, ...]
  source_names: tuple[str, ...]

  def coverage(self) -> dict[str, dict[str, int]]:
    """Counts how often each source supplied each field.

    This is the number precedence is tuned on: a source that never
    returns an ISRC should not lead the ISRC ordering, however good it is
    at everything else.

    Returns:
      Field name to source name to the number of tracks that source gave
      a value for.
    """
    counts = {
        field: {
            source: 0 for source in self.source_names
        } for field in COMPARED_FIELDS
    }
    for probe in self.probes:
      for field in COMPARED_FIELDS:
        for source in self.source_names:
          if probe.value(source, field) is not None:
            counts[field][source] += 1
    return counts

  def populated_fields(self) -> tuple[str, ...]:
    """Returns the fields at least one source had something to say about.

    Returns:
      Field names, in the canonical field order.
    """
    coverage = self.coverage()
    return tuple(
        field for field in COMPARED_FIELDS if any(coverage[field].values()))

  def art_coverage(self) -> dict[str, int]:
    """Counts how often each source offered cover art.

    Returns:
      Source name to the number of tracks it gave an art URL for.
    """
    counts = {source: 0 for source in self.source_names}
    for probe in self.probes:
      for source in self.source_names:
        result = probe.results.get(source)
        if result is not None and result.art_url:
          counts[source] += 1
    return counts

  def failures(self) -> dict[str, int]:
    """Counts how many tracks each source failed outright on.

    Returns:
      Source name to failure count, omitting sources that never failed.
    """
    counts: dict[str, int] = {}
    for probe in self.probes:
      for source in probe.errors:
        counts[source] = counts.get(source, 0) + 1
    return counts


def query_for_file(path: pathlib.Path,
                   duration_seconds: float | None = None) -> SourceQuery:
  """Builds a source query from a file's existing tags.

  Args:
    path: The audio file.
    duration_seconds: The file's duration, if already known. Read from
      the file when not given, since duration is the strongest signal for
      telling a studio cut from a live version of the same song.

  Returns:
    The query.

  Raises:
    TagError: If the file cannot be read.
  """
  duration = duration_seconds
  if duration is None:
    duration = tag_io.read_duration(path)
  return SourceQuery.from_tags(tag_io.read_tags(path), duration)


def probe_query(query: SourceQuery,
                probe_sources: Sequence[MetadataSource],
                path: pathlib.Path | None = None) -> TrackProbe:
  """Asks every source about one track.

  A source that fails is recorded and skipped rather than ending the
  probe: comparing three sources is still useful when the fourth is down.

  Args:
    query: What is known about the track.
    probe_sources: The sources to ask.
    path: The file the query came from, if any.

  Returns:
    What each source said.
  """
  results: dict[str, SourceResult | None] = {}
  errors: dict[str, str] = {}
  for source in probe_sources:
    if not source.is_available():
      errors[source.name] = "no credentials configured"
      results[source.name] = None
      continue
    try:
      candidates = source.search(query, limit=1)
    except SourceError as err:
      errors[source.name] = str(err)
      results[source.name] = None
      continue
    results[source.name] = candidates[0] if candidates else None
  return TrackProbe(query=query, path=path, results=results, errors=errors)
