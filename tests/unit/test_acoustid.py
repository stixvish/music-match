"""AcoustID response parsing (tasks/todo.md t18).

Cassette-style: the payloads below are real AcoustID responses, trimmed.
"""

from music.sources.acoustid import META, AcoustIdMatch, parse_response

# real response for "24kGoldn - Mood": one fingerprint, five near-duplicate
# musicbrainz entries for the same song.
MOOD = {
  "status": "ok",
  "results": [
    {
      "id": "e5747319-0000-0000-0000-000000000000",
      "score": 0.982,
      "recordings": [
        {
          "id": "16c83afd-1111",
          "title": "Mood",
          "artists": [{"name": "24kGoldn"}, {"name": "iann dior"}],
        },
        {
          "id": "17952e7c-2222",
          "title": "Mood",
          "artists": [{"name": "24kGoldn"}, {"name": "iann dior"}],
        },
        {
          "id": "2e620e8a-3333",
          "title": "Mood",
          "artists": [{"name": "24kGoldn"}, {"name": "iann dior"}],
        },
      ],
    }
  ],
}

# real response for a music-video rip: audio recognised, nothing linked.
VIDEO_RIP = {
  "status": "ok",
  "results": [{"id": "a508ed51-0000", "score": 0.997}],
}

EMPTY = {"status": "ok", "results": []}


def test_meta_is_space_separated():
  """The meta list must be space-separated.

  A "+" urlencodes to %2B and is silently ignored, returning status=ok with no
  metadata at all — indistinguishable from an unlinked fingerprint.
  """
  assert "+" not in META
  assert META.split() == ["recordings", "releasegroups"]


def test_parses_linked_recordings():
  matches = parse_response(MOOD)
  assert len(matches) == 1
  match = matches[0]
  assert match.score == 0.982
  assert match.title == "Mood"
  assert match.artist == "24kGoldn"
  assert match.is_linked


def test_duplicate_recording_ids_are_collapsed():
  """Five MBIDs for one song must not later read as five rival candidates."""
  payload = {
    "status": "ok",
    "results": [
      {
        "id": "x",
        "score": 0.9,
        "recordings": [
          {"id": "same", "title": "T"},
          {"id": "same", "title": "T"},
          {"id": "other", "title": "T"},
        ],
      }
    ],
  }
  assert parse_response(payload)[0].recording_ids == ("same", "other")


def test_unlinked_fingerprint_is_not_a_usable_identity():
  """A video rip scores 0.997 with zero linked MBIDs — recognised, not resolved."""
  match = parse_response(VIDEO_RIP)[0]
  assert match.score == 0.997
  assert not match.is_linked
  assert match.recording_ids == ()


def test_empty_results():
  assert parse_response(EMPTY) == []


def test_results_are_sorted_by_score():
  payload = {
    "status": "ok",
    "results": [
      {"id": "low", "score": 0.4, "recordings": [{"id": "a"}]},
      {"id": "high", "score": 0.9, "recordings": [{"id": "b"}]},
    ],
  }
  assert [m.acoustid for m in parse_response(payload)] == ["high", "low"]


def test_missing_fields_do_not_crash():
  assert parse_response({"status": "ok", "results": [{}]})[0] == AcoustIdMatch(
    acoustid="", score=0.0
  )
