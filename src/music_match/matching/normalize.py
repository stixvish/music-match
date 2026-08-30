"""Normalising titles and artists before comparing them.

Every source writes the same recording differently: "Around the World",
"Around The World", "Around the World (Radio Edit)",
"Daft_Punk_-_Around_the_World_Official_Video". Comparing those raw makes
a correct match look wrong, so both sides are reduced to a common shape
first.

The rules here are deliberately conservative. Stripping too much makes
genuinely different recordings look identical — "Strobe" and
"Strobe (DJ Marky Remix)" must not collapse onto each other, because
picking the wrong one writes wrong tags onto a file.
"""

import re
import unicodedata

# Bracketed qualifiers that describe the *same* recording and can be
# dropped when comparing. Anything implying a different recording — remix,
# live, edit, version — is deliberately absent.
_NOISE_PHRASES = (
    "official video",
    "official music video",
    "official audio",
    "official lyric video",
    "lyric video",
    "audio only",
    "hq",
    "hd",
    "4k",
)

# Featured-artist markers, which sources disagree about constantly.
#
# The word boundaries are load-bearing: without the leading one, "ft"
# matches inside "Daft Punk" and everything after it is stripped, turning
# "Daft Punk - Around the World" into "Da".
#
# "with" is deliberately absent. It is a real feature marker in some
# titles, but stripping a trailing "with ..." would also gut ordinary
# titles like "Sing With Me", and writing wrong tags is worse than
# missing a match.
_FEATURE_PATTERN = re.compile(
    r"\s*[\(\[]?\s*\b(?:feat|featuring|ft)\b\.?\s+[^\)\]]*[\)\]]?\s*$",
    re.IGNORECASE)

_BRACKETS = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def strip_accents(text: str) -> str:
  """Reduces accented characters to their base letters.

  Args:
    text: The text to fold.

  Returns:
    The text with combining marks removed, so "Röyksopp" compares equal
    to "Royksopp".
  """
  decomposed = unicodedata.normalize("NFKD", text)
  return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def strip_noise(text: str) -> str:
  """Removes bracketed phrases that do not identify a recording.

  Only phrases describing the *upload* are dropped — "Official Video" and
  friends. A bracket holding anything else is kept, because "(Live)" and
  "(Radio Edit)" are the difference between two real recordings.

  Args:
    text: The raw title.

  Returns:
    The title without upload noise.
  """

  def replace(match: re.Match[str]) -> str:
    inner = match.group(1).strip().lower()
    return "" if inner in _NOISE_PHRASES else match.group(0)

  cleaned = _BRACKETS.sub(replace, text)
  for phrase in _NOISE_PHRASES:
    cleaned = re.sub(rf"\b{re.escape(phrase)}\b",
                     " ",
                     cleaned,
                     flags=re.IGNORECASE)
  return cleaned


def strip_features(text: str) -> str:
  """Removes a trailing "feat. X" clause.

  Args:
    text: The raw title or artist.

  Returns:
    The text without its featured-artist suffix.
  """
  return _FEATURE_PATTERN.sub("", text)


def normalize(text: str | None) -> str:
  """Reduces a title or artist to a comparable form.

  Args:
    text: The raw value, or None.

  Returns:
    Lowercase alphanumerics separated by single spaces, with accents
    folded, upload noise removed and featured artists dropped. Empty for
    None.
  """
  if not text:
    return ""
  folded = strip_accents(text)
  folded = folded.replace("_", " ").replace("&", " and ")
  folded = strip_noise(folded)
  folded = strip_features(folded)
  return _NON_ALNUM.sub(" ", folded.lower()).strip()


def tokens(text: str | None) -> frozenset[str]:
  """Returns the distinct words of a normalized value.

  Args:
    text: The raw value, or None.

  Returns:
    The token set, empty for None.
  """
  return frozenset(normalize(text).split())


# Words marking a re-release of the same album rather than a different
# one. "Homework" and "Homework (25th Anniversary Edition)" are the same
# record; treating them as different answers splits the vote between
# sources that actually agree.
_EDITION_MARKERS = (
    "deluxe",
    "edition",
    "remaster",
    "remastered",
    "anniversary",
    "expanded",
    "extended mixes",
    "bonus",
    "explicit",
    "special",
    "reissue",
    "single",
    "ep",
)

_TRAILING_BRACKET = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_TRAILING_DASH = re.compile(r"\s+[-–—]\s+([^-–—]+)$")


def _is_edition(text: str) -> bool:
  """Returns whether a suffix marks an edition rather than a new release.

  Markers are matched as whole words. As substrings, short ones appear
  inside ordinary words — "ep" sits inside "Reprise" — and would strip
  the bracket off a title that is part of the record's real name.

  Args:
    text: The suffix, without its brackets or dash.

  Returns:
    True if it contains an edition marker as a whole word.
  """
  words = set(normalize(text).split())
  return any(set(marker.split()) <= words for marker in _EDITION_MARKERS)


def release_key(album: str | None) -> str:
  """Reduces an album title to something comparable across sources.

  Beyond ordinary normalisation this drops edition suffixes, so
  "For Lack of a Better Name" and
  "For Lack of A Better Name (The Extended Mixes)" compare equal. Only
  suffixes containing a known edition word are removed — a bracketed
  phrase that is part of the actual title survives.

  Args:
    album: The album title, or None.

  Returns:
    The comparable key, empty for None.
  """
  if not album:
    return ""
  trimmed = album
  for _ in range(3):
    bracket = _TRAILING_BRACKET.search(trimmed)
    if bracket and _is_edition(bracket.group(0).strip("()[] ")):
      trimmed = trimmed[:bracket.start()]
      continue
    dash = _TRAILING_DASH.search(trimmed)
    if dash and _is_edition(dash.group(1)):
      trimmed = trimmed[:dash.start()]
      continue
    break
  return normalize(trimmed) or normalize(album)
