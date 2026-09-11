"""Resolved fields to tag set (tasks/todo.md t25, SPEC.md §10)."""

from music.publish import tag
from music.publish.fields import NON_TAG_FIELDS, build


def test_every_frame_field_is_carried():
  resolved = {name: f"v-{name}" for name in tag.FRAME_MAP}
  built = build(resolved)
  assert set(built.tags.values) == set(tag.FRAME_MAP)
  assert built.skipped == ()


def test_canonical_naming_is_applied_to_the_title():
  """The database keeps the source's literal value; the file gets house style."""
  built = build({"title": "Delilah (Tom Santa Remix)"})
  assert built.tags["title"] == "Delilah [Tom Santa Remix]"


def test_feat_is_normalised_in_the_title():
  built = build({"title": "Mood (feat. iann dior)"})
  assert built.tags["title"] == "Mood (ft. iann dior)"


def test_comment_is_not_a_frame_map_field():
  built = build({"comment": "a note"})
  assert built.tags.comment == "a note"
  assert "comment" not in built.tags.values


def test_non_tag_fields_are_skipped_and_reported():
  built = build({"artwork_url": "https://x/y.jpg", "title": "T"})
  assert "artwork_url" in built.skipped
  assert "artwork_url" not in built.tags.values
  assert built.tags["title"] == "T"


def test_unknown_field_is_skipped_not_fatal():
  built = build({"invented_field": "x"})
  assert built.skipped == ("invented_field",)
  assert built.tags.values == {}


def test_empty_values_are_dropped():
  built = build({"title": "T", "album": "", "genre": None})
  assert set(built.tags.values) == {"title"}


def test_no_resolved_fields():
  built = build({})
  assert built.tags.values == {}
  assert built.tags.comment is None


def test_non_tag_fields_are_declared_not_guessed():
  """These live in the database for provenance but have no ID3 frame."""
  for field in NON_TAG_FIELDS:
    assert field not in tag.FRAME_MAP
