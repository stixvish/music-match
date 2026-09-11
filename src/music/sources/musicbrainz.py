"""MusicBrainz adapter.

Free, no key, 1 req/s, and it will block requests without a descriptive
user-agent. Authoritative for credits and for distinguishing featured from
collaborating artists, which Spotify flattens into one list (SPEC.md §7).
"""

import datetime as dt
import logging
import sqlite3
import urllib.parse
import urllib.request
from collections.abc import Sequence

from pydantic import ValidationError

from music.identify import Evidence, Match, distinct_rival_gap, variant_mismatch
from music.sources import cache
from music.sources.base import FieldCandidate, Identity, ReleaseInfo
from music.sources.ratelimit import RateLimiter, with_backoff

log = logging.getLogger(__name__)

API = "https://musicbrainz.org/ws/2"
NAME = "musicbrainz"

# edition keywords, used only to break a tie on track count (SPEC.md §7).
DELUXE_WORDS = ("deluxe", "expanded", "special", "anniversary", "complete", "extended")

_EARLIEST_SANE = dt.date(1900, 1, 1)


class MusicBrainz:
  """Lookup against the MusicBrainz web service."""

  name = NAME

  def __init__(
    self,
    conn: sqlite3.Connection,
    user_agent: str = "music-match/0.1 ( https://github.com/stixvish/music-match )",
  ) -> None:
    """Initialise the adapter.

    Args:
      conn: Open connection, used for the response cache.
      user_agent: Sent on every request; MusicBrainz blocks requests without one.
    """
    self._conn = conn
    self._ua = user_agent
    self._limiter = RateLimiter(per_second=1.0)

  def _get(self, path: str, params: dict[str, str]) -> dict:
    def fetch() -> dict:
      self._limiter.wait()
      url = f"{API}/{path}?{urllib.parse.urlencode(params | {'fmt': 'json'})}"
      request = urllib.request.Request(url, headers={"User-Agent": self._ua})
      with urllib.request.urlopen(request, timeout=30) as response:
        import json

        return dict(json.load(response))

    result = cache.cached(
      self._conn,
      NAME,
      (path, tuple(sorted(params.items()))),
      lambda: with_backoff(fetch),
    )
    return dict(result)

  def recording(self, mbid: str) -> dict:
    """Fetch one recording with full release data.

    Search results carry a truncated `releases` array — often a single
    arbitrary release — so anything that reasons about releases must re-fetch.

    Args:
      mbid: MusicBrainz recording id.

    Returns:
      The recording dict, with releases, release-groups and media.
    """
    return self._get(
      f"recording/{mbid}",
      {"inc": "releases+release-groups+media+artist-credits"},
    )

  def search_recordings(self, identity: Identity, limit: int = 8) -> list[dict]:
    """Search for recordings matching an identity.

    Args:
      identity: Normalised artist and title.
      limit: Maximum results.

    Returns:
      Raw recording dicts, best match first.
    """
    if identity.isrc:
      data = self._get(f"isrc/{identity.isrc}", {"inc": "releases+artist-credits"})
      return list(data.get("recordings", []))
    recordings = self._search_text(identity.artist, identity.title, limit)
    # searching the primary artist alone misses releases credited to the pair
    # ("Jake Fine & STRAIGHTUPJE"), so retry with the full credit.
    if (
      not recordings
      and identity.artist_full
      and (identity.artist_full != identity.artist)
    ):
      recordings = self._search_text(identity.artist_full, identity.title, limit)
    return recordings

  def _search_text(self, artist: str, title: str, limit: int) -> list[dict]:
    query = f'recording:"{title}" AND artist:"{artist}"'
    data = self._get("recording", {"query": query, "limit": str(limit)})
    return list(data.get("recordings", []))

  def releases_for(self, recording: dict) -> list[ReleaseInfo]:
    """Parse the releases a recording appears on.

    Args:
      recording: A raw recording dict.

    Returns:
      Parsed releases; malformed entries are skipped rather than fatal.
    """
    out: list[ReleaseInfo] = []
    for release in recording.get("releases", []) or []:
      group = release.get("release-group", {}) or {}
      media = (release.get("media") or [{}])[0]
      # the key is "tracks", plural. "track" silently yields nothing.
      tracks = media.get("tracks") or [{}]
      try:
        out.append(
          ReleaseInfo(
            release_id=str(release.get("id", "")),
            title=str(release.get("title", "")),
            track_count=int(media.get("track-count") or len(tracks) or 0),
            track_number=_as_int(tracks[0].get("number") or tracks[0].get("position")),
            disc_number=int(media.get("position") or 1),
            album_artist=_credit_name(release.get("artist-credit")),
            date=str(release.get("date") or group.get("first-release-date") or ""),
            primary_type=str(group.get("primary-type") or ""),
            secondary_types=tuple(group.get("secondary-types") or ()),
          )
        )
      except ValidationError as exc:  # pragma: no cover - defensive
        log.debug("skipping malformed release: %s", exc)
    return out

  def _best_recording(
    self, recordings: Sequence[dict], identity: Identity, probe: int = 5
  ) -> tuple[dict | None, list[ReleaseInfo]]:
    """Pick a recording *and* its releases together.

    The two choices are coupled: a recording that appears only on compilations
    dooms the album fields no matter how well it matches on duration. So the
    top few candidates are fetched in full and the first one that yields an
    album credited to the artist wins.

    Bounded at `probe` fetches because MusicBrainz allows 1 req/s.

    Args:
      recordings: Search results.
      identity: What we know, including duration.
      probe: How many candidates to fetch in full.

    Returns:
      The chosen recording and its parsed releases.
    """
    ordered = rank_recordings(recordings, identity.duration_s)[:probe]
    fallback: tuple[dict, list[ReleaseInfo]] | None = None
    for candidate in ordered:
      if not candidate.get("id"):
        continue
      try:
        full = self.recording(str(candidate["id"])) or candidate
      except Exception as exc:  # noqa: BLE001 - try the next candidate
        log.debug("recording lookup failed: %s", exc)
        continue
      releases = self.releases_for(full)
      artist = _credit_name(full.get("artist-credit")) or identity.artist
      if any(_same_artist(r, artist) and r.is_album for r in releases):
        return full, releases
      if fallback is None:
        fallback = (full, releases)
    return fallback if fallback else (None, [])

  def evaluate(self, identity: Identity) -> tuple[Match, Sequence[FieldCandidate]]:
    """Resolve a track and report how confident the identity is.

    Args:
      identity: Normalised artist and title, ideally with duration.

    Returns:
      The evidence, and the candidates it produced.
    """
    try:
      recordings = self.search_recordings(identity)
    except Exception as exc:  # noqa: BLE001 - a dead source must not stop a run
      log.warning("musicbrainz lookup failed: %s", exc)
      return Match(Evidence.NONE), []
    if not recordings:
      return Match(Evidence.NONE), []

    ranked = rank_recordings(recordings, identity.duration_s)
    top = ranked[0]
    # rivals that are the same song on another release are not ambiguity
    gap = distinct_rival_gap(
      [
        (
          str(r.get("title") or ""),
          float(r["length"]) / 1000 if r.get("length") else None,
          int(r.get("score") or 0),
        )
        for r in ranked
      ]
    )

    delta = None
    if identity.duration_s and top.get("length"):
      delta = float(top["length"]) / 1000 - identity.duration_s

    match = Match(
      evidence=Evidence.ISRC if identity.isrc else Evidence.TEXT,
      search_score=int(top.get("score") or 0),
      duration_delta_s=delta,
      rival_gap=gap,
      variant_mismatch=variant_mismatch(identity.title, str(top.get("title") or "")),
    )
    return match, self.lookup(identity)

  def credits(self, recording_id: str) -> list[FieldCandidate]:
    """Fetch composer and lyricist via work relationships.

    MusicBrainz keeps writer credits on the *work*, not the recording, so this
    is a two-hop lookup: `recording -> performance relation -> work`, then
    `work -> artist relations`. That is two extra requests per track at 1 req/s
    — roughly 78 minutes across a 2,300-track library — which is why it is
    opt-in. Composer and lyricist are nice-to-haves (SPEC.md §2).

    Args:
      recording_id: MusicBrainz recording MBID.

    Returns:
      Composer and lyricist candidates, empty when the work has no credits.
    """
    try:
      recording = self._get(f"recording/{recording_id}", {"inc": "work-rels"})
    except Exception as exc:  # noqa: BLE001 - credits are optional
      log.debug("work-rels lookup failed: %s", exc)
      return []

    work_ids = [
      rel["work"]["id"]
      for rel in recording.get("relations", []) or []
      if (rel.get("work") or {}).get("id")
    ]
    if not work_ids:
      return []
    try:
      work = self._get(f"work/{work_ids[0]}", {"inc": "artist-rels"})
    except Exception as exc:  # noqa: BLE001 - credits are optional
      log.debug("work artist-rels lookup failed: %s", exc)
      return []
    return parse_credits(work)

  def lookup(self, identity: Identity) -> Sequence[FieldCandidate]:
    """Return candidates for a track.

    Args:
      identity: What we know so far.

    Returns:
      Candidates, empty on a miss. Never raises for a simple miss.
    """
    try:
      recordings = self.search_recordings(identity)
    except Exception as exc:  # noqa: BLE001 - a dead source must not stop a run
      log.warning("musicbrainz lookup failed: %s", exc)
      return []
    if not recordings:
      return []

    top, releases = self._best_recording(recordings, identity)
    if top is None:
      return []
    return self._candidates(top, releases, identity)

  def candidates_from(
    self, recording: dict, identity: Identity
  ) -> Sequence[FieldCandidate]:
    """Build candidates from an already-fetched recording.

    Used by the fingerprint path, which arrives with an mbid rather than a
    search result.

    Args:
      recording: A full recording dict.
      identity: What we know so far.

    Returns:
      Candidates.
    """
    return self._candidates(recording, self.releases_for(recording), identity)

  def _candidates(
    self,
    top: dict,
    releases: list[ReleaseInfo],
    identity: Identity,  # noqa: ARG002 - kept for symmetry with the callers
  ) -> Sequence[FieldCandidate]:
    candidates = [
      FieldCandidate(field="title", value=str(top.get("title", "")), source=NAME),
      FieldCandidate(
        field="artist", value=_credit_name(top.get("artist-credit")), source=NAME
      ),
    ]
    if top.get("id"):
      candidates.append(
        FieldCandidate(field="mb_recording_id", value=str(top["id"]), source=NAME)
      )

    chosen = select_release(releases, _credit_name(top.get("artist-credit")))
    if chosen:
      # album fields come from one release, atomically (SPEC.md §7)
      for field_name, value in (
        ("album", chosen.title),
        ("album_artist", chosen.album_artist),
        ("track_number", chosen.track_number),
        ("disc_number", chosen.disc_number),
      ):
        if value:
          candidates.append(
            FieldCandidate(field=field_name, value=str(value), source=NAME)
          )

    earliest = earliest_date(releases)
    if earliest:
      candidates.append(
        FieldCandidate(field="release_date", value=earliest, source=NAME)
      )
      candidates.append(FieldCandidate(field="year", value=earliest[:4], source=NAME))
    return [c for c in candidates if c.value]


def rank_recordings(
  recordings: Sequence[dict], duration_s: float | None = None
) -> list[dict]:
  """Order search results by how likely each is to be our track.

  A search for one song routinely returns eight recordings all scoring 99-100 —
  album version, radio edit, live take, karaoke — differing only in length.
  Duration decides when known; score and release count break ties.

  Args:
    recordings: Raw search results.
    duration_s: Known duration of our audio, if any.

  Returns:
    A new list, most likely first.
  """
  target_ms = (duration_s or 0) * 1000

  def key(rec: dict) -> tuple[float, int, int]:
    length = rec.get("length")
    delta = abs(float(length) - target_ms) if (length and target_ms) else float("inf")
    return (delta, -int(rec.get("score") or 0), -len(rec.get("releases") or []))

  return sorted(recordings, key=key)


def parse_credits(work: dict) -> list[FieldCandidate]:
  """Extract composer and lyricist from a work's artist relations.

  MusicBrainz distinguishes `composer`, `lyricist` and the combined `writer`.
  A bare `writer` credit is used for composer only when no explicit composer
  exists, since it means "wrote it" without saying which half.

  Args:
    work: A work object fetched with `inc=artist-rels`.

  Returns:
    Candidates, empty when the work carries no credits.
  """
  by_type: dict[str, list[str]] = {}
  for rel in work.get("relations", []) or []:
    name = (rel.get("artist") or {}).get("name")
    kind = str(rel.get("type") or "").casefold()
    if name and kind in ("composer", "lyricist", "writer"):
      by_type.setdefault(kind, []).append(str(name))

  out: list[FieldCandidate] = []
  composer = by_type.get("composer") or by_type.get("writer") or []
  if composer:
    out.append(FieldCandidate(field="composer", value=", ".join(composer), source=NAME))
  lyricist = by_type.get("lyricist") or []
  if lyricist:
    out.append(FieldCandidate(field="lyricist", value=", ".join(lyricist), source=NAME))
  return out


def _as_int(value: object) -> int | None:
  try:
    return int(str(value))
  except TypeError, ValueError:
    return None


def _credit_name(credit: object) -> str:
  if isinstance(credit, list) and credit:
    first = credit[0]
    if isinstance(first, dict):
      artist = first.get("artist") or {}
      name = first.get("name") or (
        artist.get("name") if isinstance(artist, dict) else ""
      )
      return str(name or "")
  return ""


def _same_artist(release: ReleaseInfo, artist: str) -> bool:
  if not artist or not release.album_artist:
    return False
  a, b = release.album_artist.casefold(), artist.casefold()
  return a == b or a.startswith(b) or b.startswith(a)


def select_release(
  releases: Sequence[ReleaseInfo], artist: str = ""
) -> ReleaseInfo | None:
  """Choose the release the album fields come from (SPEC.md §7).

  Filtering order matters, and was corrected against live data:

  1. **Album artist must match the track artist.** This is the only reliable
     way to exclude compilations. MusicBrainz types many of them as plain
     `Album` with no `Compilation` secondary type — "Hit Mania 2012" and
     "Berlin Tag & Nacht #7" both won on track count before this filter
     existed. Compilations are credited to Various Artists; real albums are not.
  2. Primary type `Album`, excluding live/remix/compilation secondaries.
  3. Most tracks, so a deluxe beats its standard.
  4. Edition keyword, as a tie-break only.

  Args:
    releases: Candidate releases.
    artist: Track artist, used for step 1. Without it, step 1 is skipped and
      the result is much less trustworthy.

  Returns:
    The chosen release, or None if there are none.
  """
  if not releases:
    return None

  pool = [r for r in releases if _same_artist(r, artist)] if artist else []
  if not pool:
    pool = [r for r in releases if r.is_album] or list(releases)
  else:
    pool = [r for r in pool if r.is_album] or pool

  def rank(release: ReleaseInfo) -> tuple[int, int]:
    deluxe = any(word in release.title.lower() for word in DELUXE_WORDS)
    # track count first; keyword only breaks a tie (survives non-english naming)
    return (release.track_count, int(deluxe))

  return max(pool, key=rank)


def earliest_date(releases: Sequence[ReleaseInfo]) -> str:
  """Earliest plausible release date across every release (SPEC.md §7).

  Deliberately spans singles and EPs as well as albums: most singles precede
  their album, and the date describes when the music came out, not where the
  track is filed.

  Args:
    releases: Candidate releases.

  Returns:
    An ISO date string, or "" if none is usable.
  """
  today = dt.date.today()
  best: tuple[dt.date, str] | None = None
  for release in releases:
    parsed = _parse_date(release.date)
    if parsed is None or not (_EARLIEST_SANE <= parsed <= today):
      continue
    # prefer full precision when two releases share the same day
    if (
      best is None
      or parsed < best[0]
      or (parsed == best[0] and len(release.date) > len(best[1]))
    ):
      best = (parsed, release.date)
  return best[1] if best else ""


def _parse_date(raw: str) -> dt.date | None:
  if not raw:
    return None
  # musicbrainz dates come as YYYY, YYYY-MM or YYYY-MM-DD; pad to a full date
  for padded in (raw, f"{raw}-01", f"{raw}-01-01"):
    try:
      return dt.datetime.strptime(padded, "%Y-%m-%d").date()
    except ValueError:
      continue
  return None
