"""Canonical output naming (SPEC.md §14).

House style, applied identically to the TIT2 tag and the filename so search
behaves the same in Finder, rekordbox and serato:

  features use `ft.`, in parentheses     Mood (ft. iann dior)
  remixes use square brackets            Delilah [Tom Santa Remix]
  both may co-occur, features first      Title (ft. Guest) [Someone Remix]

This is *output* formatting. It is the opposite end of the pipeline from query
normalisation, which strips titles down so sources can match them. Do not
conflate the two.
"""

import re
import unicodedata
from collections.abc import Sequence

# "feat.", "featuring", "ft" -> "ft."
_FEAT = re.compile(r"\b(?:feat\.?|featuring|ft\.?)\s+", re.IGNORECASE)
# a trailing "(...)" or "[...]" naming a remix, edit, mix, bootleg or version
_MIX = re.compile(
  r"[\(\[]\s*([^)\]]*\b(?:remix|edit|mix|bootleg|flip|vip|version|rework)\b"
  r"[^)\]]*)\s*[\)\]]",
  re.IGNORECASE,
)
# " - Something Remix" (the dash form beatport and youtube both use)
_MIX_DASH = re.compile(
  r"\s+[-–]\s+([^-–]*\b(?:remix|edit|mix|bootleg|flip|vip|version|rework)\b"
  r"[^-–]*)$",
  re.IGNORECASE,
)
_FEAT_GROUP = re.compile(r"[\(\[]\s*ft\.\s*([^)\]]+)\s*[\)\]]", re.IGNORECASE)
_ILLEGAL = re.compile(r"[/:\x00-\x1f]")
# liberal on read: every separator seen in real tags
_ARTIST_SPLIT = re.compile(r"\s*(?:;|&|,| x |\bvs\.?\b|\band\b)\s*", re.IGNORECASE)
_SPACES = re.compile(r"\s+")


def canonical_title(raw: str) -> str:
  """Rewrite a title into house style.

  Args:
    raw: Title as the winning source supplied it.

  Returns:
    The title with `ft.` parenthesised and any mix name bracketed.
  """
  text = _SPACES.sub(" ", (raw or "").strip())
  if not text:
    return ""

  mix = ""
  match = _MIX.search(text)
  if match:
    mix = match.group(1).strip()
    text = text[: match.start()] + text[match.end() :]
  else:
    dash = _MIX_DASH.search(text)
    if dash:
      mix = dash.group(1).strip()
      text = text[: dash.start()]

  text = _FEAT.sub("ft. ", text)
  feat = ""
  feat_match = _FEAT_GROUP.search(text)
  if feat_match:
    feat = feat_match.group(1).strip()
    text = text[: feat_match.start()] + text[feat_match.end() :]
  else:
    bare = re.search(r"\bft\.\s+(.+)$", text)
    if bare:
      feat = bare.group(1).strip()
      text = text[: bare.start()]

  base = _SPACES.sub(" ", text).strip(" -–")
  parts = [base]
  if feat:
    parts.append(f"(ft. {feat})")
  if mix:
    parts.append(f"[{mix}]")
  return " ".join(p for p in parts if p)


def safe_component(raw: str) -> str:
  """Make a string safe to use as one path component.

  The tag keeps the true value; only the filename is sanitised.

  Args:
    raw: Any string.

  Returns:
    A non-empty, filesystem-safe component.
  """
  text = unicodedata.normalize("NFC", (raw or "").strip())
  text = _ILLEGAL.sub("-", text)
  text = _SPACES.sub(" ", text).strip()
  # trailing dots are legitimate in artist names ("Fred again..") and must
  # survive; only a component that is *entirely* dots is unsafe, since "." and
  # ".." are path traversal.
  if set(text) <= {"."}:
    return "unknown"
  return text[:120].strip() or "unknown"


def format_artists(artists: Sequence[str]) -> str:
  """Render a credited-artist list in house style (SPEC.md §14).

  One rule, serial-comma form::

      ["A"]           -> "A"
      ["A", "B"]      -> "A & B"
      ["A", "B", "C"] -> "A, B & C"

  Two artists collapse to `A & B` naturally, so this is a single rule rather
  than a special case. It matches MusicBrainz's and Apple's own convention.

  Order comes from the catalogue's artist credit, primary first — never
  alphabetical.

  This covers *credited* artists only. Featured artists live in the title as
  `(ft. X)` and are never merged into this list.

  Args:
    artists: Credited artists, primary first.

  Returns:
    The formatted credit.
  """
  names = [a.strip() for a in artists if a and a.strip()]
  if not names:
    return ""
  if len(names) == 1:
    return names[0]
  return f"{', '.join(names[:-1])} & {names[-1]}"


def split_artists(raw: str) -> list[str]:
  """Parse an artist string written in any convention we have encountered.

  Input tags are inconsistent — the existing library uses `;` on 427 tracks,
  `&` on 50 and `,` on 46, sometimes mixed in one string. Reading must be
  liberal even though writing is strict.

  Args:
    raw: An artist tag or credit line.

  Returns:
    Individual artist names, order preserved.
  """
  parts = _ARTIST_SPLIT.split(raw or "")
  return [p.strip() for p in parts if p and p.strip()]


def filename(artist: str, title: str, suffix: str = ".aiff") -> str:
  """Build the output filename.

  Args:
    artist: Artist name.
    title: Title, already canonical or raw.
    suffix: File extension including the dot.

  Returns:
    `Artist - Title (ft. X) [Y Remix].aiff`
  """
  return f"{safe_component(artist)} - {safe_component(canonical_title(title))}{suffix}"
