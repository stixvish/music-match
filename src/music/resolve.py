"""Resolve a track's identity, strongest evidence first (SPEC.md §12).

Order: manual url override, ISRC, acoustic fingerprint, normalised text search.
Each step is tried only when the one above it produces nothing usable.
"""

import logging
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from music.identify import (
  PLAUSIBLE,
  Evidence,
  Match,
  artist_is_unrelated,
  is_plausible,
  match_score,
  should_auto_accept,
  variant_mismatch,
)
from music.publish.naming import strip_version
from music.sources.acoustid import AcoustId
from music.sources.base import FieldCandidate, Identity
from music.sources.musicbrainz import (
  MusicBrainz,
  recording_artist,
  select_release,
)

log = logging.getLogger(__name__)

# how many of acoustid's linked mbids to inspect. a single fingerprint can map
# to a dozen near-duplicate musicbrainz entries for the same song.
MBID_PROBE = 3

# A release credited to the searched artist is the strongest single signal
# that the fingerprint linked to the right recording, so it outweighs a
# moderate difference in how the title is written.
RELEASE_MATCH_BONUS = 0.25


@dataclass
class Resolution:
  """The outcome of resolving one track."""

  match: Match
  candidates: list[FieldCandidate] = field(default_factory=list)
  acoustid: str = ""
  mb_recording_id: str = ""


class Resolver:
  """Runs the evidence ladder over the available sources."""

  def __init__(
    self,
    conn: sqlite3.Connection,
    musicbrainz: MusicBrainz,
    acoustid: AcoustId | None = None,
    extra: Sequence[object] = (),
  ) -> None:
    """Initialise the resolver.

    Args:
      conn: Open connection.
      musicbrainz: MusicBrainz adapter, which supplies identity.
      acoustid: AcoustID adapter, if a key is configured.
      extra: Additional sources — discogs, spotify, itunes. These enrich
        **fields**; they do not establish identity, which comes from the
        fingerprint or the MusicBrainz match above.
    """
    self._conn = conn
    self._mb = musicbrainz
    self._acoustid = acoustid
    self._extra = list(extra)

  def resolve(self, identity: Identity, audio: Path | None = None) -> Resolution:
    """Identify a track.

    Args:
      identity: Normalised artist and title, ideally with duration.
      audio: The audio file, required for fingerprinting.

    Returns:
      The resolution, including the evidence behind it.
    """
    resolved: Resolution | None = None
    if audio is not None and self._acoustid is not None:
      resolved = self._by_fingerprint(identity, audio)
    if resolved is None:
      match, candidates = self._mb.evaluate(identity)
      resolved = Resolution(match=match, candidates=list(candidates))

    # enrichment only runs once identity is settled: asking five services about
    # a track we cannot identify produces five confident answers about the
    # wrong song.
    if resolved.match.evidence is not Evidence.NONE:
      enrichment = self._enrich(identity)
      resolved.candidates.extend(enrichment)
      # A doubtful identity can still be settled by the catalogues that were
      # queried independently. This runs *after* enrichment for the reason
      # above, and only ever raises confidence.
      if not should_auto_accept(resolved.match) and corroborates(identity, enrichment):
        resolved.match = replace(resolved.match, corroborated=True)
    return resolved

  def _enrich(self, identity: Identity) -> list[FieldCandidate]:
    """Collect field candidates from the non-identity sources."""
    out: list[FieldCandidate] = []
    for source in self._extra:
      try:
        out.extend(source.lookup(identity))  # type: ignore[attr-defined]
      except Exception as exc:  # noqa: BLE001 - one dead source is not fatal
        log.warning("enrichment source failed: %s", exc)
    return out

  def _by_fingerprint(self, identity: Identity, audio: Path) -> Resolution | None:
    matches = self._acoustid.identify(audio) if self._acoustid else []
    if not matches:
      return None
    top = matches[0]

    # An AcoustID entry links many recordings in no meaningful order, so every
    # probed one is scored against the text identity rather than the first
    # being taken. A matching *release* is still the strongest signal, but it
    # is no longer the only thing checked: "Last Night" matched a recording
    # whose releases were all wrong, and the first recording — Metro Station's
    # "California" — was kept anyway (SPEC.md §12).
    scored: list[tuple[float, int, dict]] = []
    for index, mbid in enumerate(top.recording_ids[:MBID_PROBE]):
      try:
        recording = self._mb.recording(mbid)
      except Exception as exc:  # noqa: BLE001 - try the next mbid
        log.debug("mbid lookup failed for %s: %s", mbid[:8], exc)
        continue
      score = match_score(
        identity.artist,
        identity.title,
        recording_artist(recording),
        str(recording.get("title") or ""),
      )
      if select_release(self._mb.releases_for(recording), identity.artist):
        score += RELEASE_MATCH_BONUS
      scored.append((score, -index, recording))

    if not scored:
      return None
    score, _, recording = max(scored, key=lambda item: (item[0], item[1]))
    if score < PLAUSIBLE:
      # nothing the fingerprint linked to resembles what we searched for.
      # falling through to the first recording is how twelve tracks were
      # published as the wrong song at confidence 0.95.
      log.info(
        "acoustid match rejected for %s - %s: best linked recording scored %.2f",
        identity.artist[:30],
        identity.title[:40],
        score,
      )
      return None
    candidates = list(self._mb.candidates_from(recording, identity))
    delta = None
    if identity.duration_s and recording.get("length"):
      delta = float(recording["length"]) / 1000 - identity.duration_s

    match = Match(
      evidence=Evidence.ACOUSTID,
      acoustid_score=top.score,
      # the fingerprint is computed over this exact file, so when the linked
      # recording has no length we treat the audio itself as the duration
      # evidence rather than penalising a strong acoustic match.
      duration_delta_s=0.0 if delta is None else delta,
      variant_mismatch=variant_mismatch(
        identity.title, str(recording.get("title") or "")
      ),
      artist_unrelated=artist_is_unrelated(
        identity.artist, recording_artist(recording)
      ),
    )
    return Resolution(
      match=match,
      candidates=candidates,
      acoustid=top.acoustid,
      mb_recording_id=str(recording.get("id") or ""),
    )


def to_identity(
  artist: str, title: str, duration_s: float | None, artist_full: str = ""
) -> Identity:
  """Build an Identity from normalised parts.

  Args:
    artist: Primary artist.
    title: Cleaned title.
    duration_s: Duration of our audio.
    artist_full: Full credit, for retry.

  Returns:
    An Identity.
  """
  return Identity(
    artist=artist, title=title, duration_s=duration_s, artist_full=artist_full
  )


def candidate_map(candidates: Sequence[FieldCandidate]) -> dict[str, str]:
  """Flatten candidates into a field->value map, first value winning.

  Args:
    candidates: Candidates from a source.

  Returns:
    A plain mapping, for display and tests.
  """
  out: dict[str, str] = {}
  for candidate in candidates:
    out.setdefault(candidate.field, candidate.value)
  return out


def corroborates(
  identity: Identity,
  candidates: Sequence[FieldCandidate],
  *,
  min_sources: int = 2,
) -> bool:
  """Whether independent catalogues agree on this track's identity.

  Two conditions, both required. The sources must agree **with each other**,
  and that agreement must also match **what we searched for** — without the
  second, two catalogues confidently describing the same wrong song would
  confirm each other.

  Version qualifiers are ignored when comparing sources to each other, since
  one catalogue listing `(feat. X)` and another not is not a disagreement about
  which song this is. Whether the *cut* is right is a separate question, and
  `variant_mismatch` keeps its veto over this (SPEC.md §12).

  Args:
    identity: What we searched for.
    candidates: Candidates from the enrichment sources only.
    min_sources: How many distinct sources must agree.

  Returns:
    True when the identity is corroborated.
  """
  by_source: dict[str, dict[str, str]] = {}
  for candidate in candidates:
    if candidate.field in ("artist", "title") and candidate.value:
      by_source.setdefault(candidate.source, {})[candidate.field] = candidate.value

  groups: dict[tuple[str, str], list[str]] = {}
  for source, values in by_source.items():
    if "artist" not in values or "title" not in values:
      continue
    key = (
      _fold(values["artist"]),
      _fold(strip_version(values["title"])),
    )
    groups.setdefault(key, []).append(source)

  for sources in groups.values():
    if len(sources) < min_sources:
      continue
    agreed = by_source[sources[0]]
    if is_plausible(
      identity.artist, identity.title, agreed["artist"], agreed["title"]
    ) and not artist_is_unrelated(identity.artist, agreed["artist"]):
      return True
  return False


def _fold(text: str) -> str:
  return re.sub(r"[^a-z0-9]", "", (text or "").casefold())
