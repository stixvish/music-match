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


# --- the ui offers every field the tag can hold ----------------------------


def test_every_writable_field_is_offered_in_the_ui():
  """A field you cannot see is a field you cannot fill in.

  The ui kept its own hand-written list and drifted from the tag writer:
  `grouping` and `original_artist` were writable but never shown, so no value
  could be typed into them.
  """
  from music.publish.fields import EDITABLE_ORDER
  from music.publish.tag import FRAME_MAP

  assert set(FRAME_MAP) <= set(EDITABLE_ORDER)


def test_nothing_unwritable_is_offered():
  """Everything offered must be able to reach the file.

  `comment` is writable but absent from FRAME_MAP — `build` handles it
  specially — so deriving the list from FRAME_MAP alone silently dropped a
  field that both Rekordbox and Serato display.
  """
  from music.publish.fields import EDITABLE_ORDER, EXTRA_WRITABLE
  from music.publish.tag import FRAME_MAP

  unexplained = set(EDITABLE_ORDER) - set(FRAME_MAP) - set(EXTRA_WRITABLE)
  assert unexplained == {"artwork_url"}


def test_comment_reaches_the_tag():
  """It renders in both apps, so it has to survive `build`."""
  from music.publish.fields import build

  assert build({"comment": "cue at 1:04"}).tags.comment == "cue at 1:04"


def test_the_groups_cover_the_order_exactly():
  from music.publish.fields import EDITABLE_ORDER, FIELD_GROUPS

  flat = [f for _name, names in FIELD_GROUPS for f in names]
  assert flat == list(EDITABLE_ORDER)
  assert len(flat) == len(set(flat)), "a field appears in two groups"
