"""Tests for the source comparison the probe produces."""

import pathlib

import pytest

from music_match import probe as probe_lib
from music_match.sources import base
from music_match.tagging.fields import TrackTags


class StubSource(base.MetadataSource):
  """A source returning a fixed answer, for testing the comparison."""

  def __init__(self,
               name: str,
               tags: TrackTags | None = None,
               *,
               available: bool = True,
               fails: bool = False,
               art_url: str | None = None) -> None:
    """Records what this stub should do.

    Args:
      name: The source name.
      tags: The tags to return, or None to return no candidates.
      available: Whether the source reports credentials.
      fails: Whether searching raises.
      art_url: Cover art URL to report.
    """
    self.name = name
    self._tags = tags
    self._available = available
    self._fails = fails
    self._art_url = art_url

  def is_available(self) -> bool:
    """Returns whether this stub claims to be usable."""
    return self._available

  def search(self,
             query: base.SourceQuery,
             limit: int = 3) -> list[base.SourceResult]:
    """Returns the canned result.

    Args:
      query: Ignored.
      limit: Ignored.

    Returns:
      The canned candidate, or none.

    Raises:
      SourceError: If this stub was told to fail.
    """
    del query, limit
    if self._fails:
      raise base.SourceError(f"{self.name} exploded")
    if self._tags is None:
      return []
    return [
        base.SourceResult(source=self.name,
                          source_id="1",
                          tags=self._tags,
                          art_url=self._art_url)
    ]


QUERY = base.SourceQuery(title="Strobe", artist="deadmau5")


def test_probe_collects_every_source() -> None:
  """Each source's own top candidate is recorded under its name."""
  probe = probe_lib.probe_query(QUERY, [
      StubSource("a", TrackTags(title="Strobe")),
      StubSource("b", TrackTags(title="Strobe (Radio Edit)")),
  ])
  assert probe.value("a", "title") == "Strobe"
  assert probe.value("b", "title") == "Strobe (Radio Edit)"


def test_a_source_with_no_answer_reports_none() -> None:
  """A source that found nothing is recorded as having nothing."""
  probe = probe_lib.probe_query(QUERY, [StubSource("a", None)])
  assert probe.value("a", "title") is None


def test_a_failing_source_does_not_end_the_probe() -> None:
  """Comparing three sources is still useful when the fourth is down."""
  probe = probe_lib.probe_query(
      QUERY,
      [StubSource("a", fails=True),
       StubSource("b", TrackTags(title="Strobe"))])
  assert "a" in probe.errors
  assert probe.value("b", "title") == "Strobe"


def test_an_unavailable_source_is_reported_not_queried() -> None:
  """Missing credentials are explained rather than silently skipped."""
  probe = probe_lib.probe_query(QUERY, [StubSource("a", available=False)])
  assert "no credentials" in probe.errors["a"]


def report_for(*sources: base.MetadataSource) -> probe_lib.ProbeReport:
  """Probes one query against the given sources.

  Args:
    *sources: The sources to ask.

  Returns:
    The finished report.
  """
  names = tuple(source.name for source in sources)
  probe = probe_lib.probe_query(QUERY, list(sources))
  return probe_lib.ProbeReport(probes=(probe,), source_names=names)


def test_coverage_counts_who_supplied_what() -> None:
  """Coverage is the number precedence is tuned on."""
  report = report_for(
      StubSource("withisrc", TrackTags(title="S", isrc="X")),
      StubSource("noisrc", TrackTags(title="S")),
  )
  coverage = report.coverage()
  assert coverage["isrc"]["withisrc"] == 1
  assert coverage["isrc"]["noisrc"] == 0
  assert coverage["title"] == {"withisrc": 1, "noisrc": 1}


def test_populated_fields_skips_what_nobody_answered() -> None:
  """Fields no source filled are noise in the table."""
  report = report_for(StubSource("a", TrackTags(title="S")))
  populated = report.populated_fields()
  assert "title" in populated
  assert "isrc" not in populated


def test_source_video_id_is_never_compared() -> None:
  """It is internal bookkeeping; no external source can know it."""
  assert "source_video_id" not in probe_lib.COMPARED_FIELDS


def test_art_coverage_is_counted_separately() -> None:
  """Album art is not a tag field but is a reason to prefer a source."""
  report = report_for(
      StubSource("witharT", TrackTags(title="S"), art_url="https://x/a.jpg"),
      StubSource("noart", TrackTags(title="S")),
  )
  assert report.art_coverage() == {"witharT": 1, "noart": 0}


def test_failures_are_counted() -> None:
  """A source that failed is reported rather than reading as empty."""
  report = report_for(StubSource("a", fails=True),
                      StubSource("b", TrackTags(title="S")))
  assert report.failures() == {"a": 1}


def test_probe_label_prefers_the_file_name(tmp_path: pathlib.Path) -> None:
  """A probe of a file is identified by that file."""
  probe = probe_lib.probe_query(QUERY, [], tmp_path / "track.m4a")
  assert probe.label() == "track.m4a"


def test_probe_label_falls_back_to_the_query() -> None:
  """An ad-hoc probe is identified by what was asked."""
  probe = probe_lib.probe_query(QUERY, [])
  assert probe.label() == "deadmau5 Strobe"


def test_query_from_file_reads_its_tags(m4a_file: pathlib.Path) -> None:
  """A file's existing tags become the search query."""
  from music_match.tagging import tags as tag_io  # pylint: disable=import-outside-toplevel
  tag_io.write_tags(m4a_file, TrackTags(title="Strobe", artist="deadmau5"))
  query = probe_lib.query_for_file(m4a_file)
  assert query.title == "Strobe"
  assert query.artist == "deadmau5"


def test_unusable_query_is_not_sent() -> None:
  """Without a title there is nothing to search on."""
  assert not base.SourceQuery(artist="deadmau5").is_usable()


@pytest.mark.parametrize("field", ["remixer", "mix_name", "isrc", "genre"])
def test_fields_precedence_cares_about_are_compared(field: str) -> None:
  """The fields precedence.toml overrides must be in the comparison."""
  assert field in probe_lib.COMPARED_FIELDS
