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
from dataclasses import dataclass

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


_TYPOGRAPHIC = str.maketrans(
  {
    "\u2019": "'",
    "\u2018": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2010": "-",  # HYPHEN: MusicBrainz writes "Anne\u2010Marie" with it
    "\u2011": "-",  # non-breaking hyphen
    "\u2013": "-",
    "\u2014": "-",
  }
)


def asciify_quotes(raw: str) -> str:
  """Replace typographic quotes and dashes with ASCII equivalents.

  MusicBrainz uses typographic quotes, so "Club Can\u2019t Handle Me" never
  matches a DJ-software search for "Can't".

  Args:
    raw: Any text.

  Returns:
    The text with curly quotes and en/em dashes normalised.
  """
  return (raw or "").translate(_TYPOGRAPHIC)


# An uppercase letter straight after an apostrophe, but only where it forms a
# real contraction: `I'Ll`, `Don'T`, `It'S`. Spotify title-cases aggressively
# enough to produce these and they are never correct English, so they are
# repaired on the way out rather than fought over in precedence.
#
# The suffix list is explicit because a blanket rule destroys names:
# `O'Brien` is not a contraction and must survive untouched.
_CONTRACTION = re.compile(
  r"(['\u2019])(LL|RE|VE|S|T|D|M|N)\b",
  re.IGNORECASE,
)


def fix_contractions(raw: str) -> str:
  """Lower-case a letter that title-casing wrongly capitalised after an apostrophe.

  Args:
    raw: Any text.

  Returns:
    The text with `I'Ll` repaired to `I'll`.
  """
  return _CONTRACTION.sub(lambda m: m.group(1) + m.group(2).lower(), raw or "")


def canonical_title(raw: str) -> str:
  """Rewrite a title into house style.

  Args:
    raw: Title as the winning source supplied it.

  Returns:
    The title with `ft.` parenthesised and any mix name bracketed.
  """
  text = _SPACES.sub(" ", fix_contractions(asciify_quotes(raw)).strip())
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
  text = unicodedata.normalize("NFC", asciify_quotes(raw).strip())
  text = _ILLEGAL.sub("-", text)
  text = _SPACES.sub(" ", text).strip()
  # trailing dots are legitimate in artist names ("Fred again..") and must
  # survive; only a component that is *entirely* dots is unsafe, since "." and
  # ".." are path traversal.
  if set(text) <= {"."}:
    return "unknown"
  return text[:120].strip() or "unknown"


# version labels that describe a cut rather than name a person. "Extended Mix"
# has no remixer; "Tom Santa Remix" does.
GENERIC_VERSIONS = (
  "extended",
  "original",
  "radio",
  "club",
  "instrumental",
  "acapella",
  "a cappella",
  "vip",
  "dub",
  "intro",
  "short",
  "clean",
  "explicit",
)

_VERSION_WORD = re.compile(
  r"\b(remix|mix|edit|rework|flip|bootleg|version|dub)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class Version:
  """A track's version designation, split into label and person."""

  mix_name: str = ""
  remixer: str = ""


def extract_version(title: str) -> Version:
  """Pull the mix name and remixer out of a title (SPEC.md §14).

  `mix_name` is *which version*; `remixer` is *who made it*. They diverge
  often — "Extended Mix" is a version with no remixer, while "Tom Santa Remix"
  is both::

      "Delilah [Tom Santa Remix]"   -> mix_name="Tom Santa Remix", remixer="Tom Santa"
      "Say Nothing - Extended Mix"  -> mix_name="Extended Mix",    remixer=""
      "Summer"                      -> mix_name="",                remixer=""

  Args:
    title: A title in canonical or raw form.

  Returns:
    The extracted version, empty when the title carries none.
  """
  text = _SPACES.sub(" ", (title or "").strip())
  if not text:
    return Version()

  label = ""
  bracketed = _MIX.search(text)
  if bracketed:
    label = bracketed.group(1).strip()
  else:
    dashed = _MIX_DASH.search(text)
    if dashed:
      label = dashed.group(1).strip()
  if not label:
    return Version()

  # the remixer is whatever precedes the version word, unless that text is not
  # a person.
  match = _VERSION_WORD.search(label)
  person = label[: match.start()].strip(" -–") if match else ""
  if not _is_person(person, match.group(1) if match else ""):
    person = ""
  return Version(mix_name=label, remixer=person)


def _is_person(text: str, version_word: str) -> bool:
  """Whether the text before a version word names a remixer.

  Rejects, in order of how often they were seen in the real library:

  - generic version labels: "Extended Mix" has no remixer
  - years: "2019 Edit" is a reissue, not a person
  - possessives before "Version": "Taylor's Version" is a re-recording by the
    original artist, not a remix by someone else

  Args:
    text: The text preceding the version word.
    version_word: The matched version word itself.

  Returns:
    True if `text` names a remixer.
  """
  candidate = text.strip()
  if not candidate:
    return False
  if any(word in candidate.casefold() for word in GENERIC_VERSIONS):
    return False
  if re.fullmatch(r"[\d\s'\u2019-]+", candidate):
    return False
  # a bare year ("2019 Edit") is not a person; a leading digit is fine,
  # "808 BEACH" is a real artist.
  # "Taylor's Version" is a re-recording by the original artist, not a remix.
  possessive_version = version_word.casefold() == "version" and bool(
    re.search(r"['\u2019]s$", candidate)
  )
  return not possessive_version


def strip_version(title: str) -> str:
  """Remove a version designation, keeping any `feat.` credit.

  The two kinds of qualifier are not alike. `(Country Mix)` names a different
  recording; `(feat. Meghan Trainor)` names the same recording more completely.
  Grouping titles for agreement has to ignore the first and respect the second.

  Args:
    title: Any title.

  Returns:
    The title without its version designation.
  """
  text = _SPACES.sub(" ", (title or "").strip())
  match = _MIX.search(text)
  if match:
    text = text[: match.start()] + text[match.end() :]
  else:
    dashed = _MIX_DASH.search(text)
    if dashed:
      text = text[: dashed.start()]
  return _SPACES.sub(" ", text).strip(" -–")


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
