"""Clean artist and title before querying any source.

Measured worth +31 percentage points of resolution accuracy — 24% to 55%
high-confidence over a 30-track MusicBrainz sample (SPEC.md §4). That is more
than adding any single extra source buys, which is why this module has no
dependencies and lands before any network code.

This is *query* normalisation: strip everything down so a catalogue can match
it. It is the opposite end of the pipeline from canonical output naming
(`publish/naming.py`), which formats the winning result for display. Do not
conflate them.
"""

import re
import unicodedata
from dataclasses import dataclass

# channel-name noise: "LMFAOVEVO" -> "LMFAO", "Artist - Topic" -> "Artist"
_VEVO = re.compile(r"\s*VEVO$", re.IGNORECASE)
_TOPIC = re.compile(r"\s*-\s*Topic$", re.IGNORECASE)
_OFFICIAL_SUFFIX = re.compile(r"\s*-\s*Official$", re.IGNORECASE)

# bracketed production noise, anywhere in the title
_NOISE = re.compile(
  r"[\(\[]\s*(?:"
  r"official\s*(?:music\s*)?(?:video|audio|visuali[sz]er)?|"
  r"music\s*video|lyrics?(?:\s*video)?|visuali[sz]er|m/?v|"
  r"audio|hd|hq|4k|full\s*(?:album|song)|out\s*now|free\s*(?:dl|download)|"
  r"premiere|explicit|clean|remaster(?:ed)?(?:\s*\d{4})?|bonus\s*track?"
  r")\s*[\)\]]",
  re.IGNORECASE,
)
# the same noise as a bare suffix. the separator is optional because
# underscored filenames lose it: "Maroon_5_-_Sugar_Official_Music_Video"
# becomes "Maroon 5 - Sugar Official Music Video" with nothing to split on.
_NOISE_BARE = re.compile(
  r"\s*[-|]?\s*(?:official\s*(?:music\s*)?video|official\s*audio"
  r"|lyrics?\s*video|visuali[sz]er|out\s*now|bonus\s*track)\s*$",
  re.IGNORECASE,
)
_FEAT = re.compile(
  r"\s*[\(\[]?\s*\b(?:feat\.?|featuring|ft\.?|with)\s+[^)\]]*[\)\]]?\s*",
  re.IGNORECASE,
)
_MULTI_ARTIST = re.compile(r"\s*(?:;|,|&| x | vs\.? | and )\s*", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


@dataclass(frozen=True)
class Normalised:
  """A cleaned query pair, plus what was stripped out."""

  artist: str
  title: str
  featured: tuple[str, ...] = ()
  # the full credit, collaborators intact. searching the primary artist alone
  # misses releases credited to the pair ("Jake Fine & STRAIGHTUPJE"), so the
  # resolver retries with this when the primary-artist query finds nothing.
  artist_full: str = ""

  @property
  def is_empty(self) -> bool:
    """Whether there is nothing left to search with."""
    return not self.artist or not self.title


def _tidy(text: str) -> str:
  text = unicodedata.normalize("NFC", text or "").translate(_QUOTES)
  text = text.replace("_", " ")
  return _SPACES.sub(" ", text).strip()


def clean_artist(raw: str) -> str:
  """Reduce a channel or artist string to the primary artist.

  Args:
    raw: Artist tag or YouTube channel name.

  Returns:
    The primary artist, with VEVO/Topic suffixes and co-artists removed.
  """
  text = _tidy(raw)
  text = _TOPIC.sub("", text)
  text = _VEVO.sub("", text)
  text = _OFFICIAL_SUFFIX.sub("", text)
  # search works better with one artist; collaborators come from the catalogue
  parts = _MULTI_ARTIST.split(text, maxsplit=1)
  return _tidy(parts[0]) if parts else text


def clean_artist_full(raw: str) -> str:
  """Clean an artist string without dropping collaborators.

  Args:
    raw: Artist tag or channel name.

  Returns:
    The full credit with channel noise removed.
  """
  text = _tidy(raw)
  text = _TOPIC.sub("", text)
  text = _VEVO.sub("", text)
  return _tidy(_OFFICIAL_SUFFIX.sub("", text))


def extract_featured(raw: str) -> tuple[str, ...]:
  """Pull featured-artist names out of a title.

  Args:
    raw: Title text.

  Returns:
    Featured artist names, in order.
  """
  names: list[str] = []
  for match in re.finditer(
    r"[\(\[]?\s*\b(?:feat\.?|featuring|ft\.?)\s+([^)\]]+?)\s*[\)\]]?\s*$",
    _tidy(raw),
    re.IGNORECASE,
  ):
    for name in _MULTI_ARTIST.split(match.group(1)):
      cleaned = _tidy(name)
      if cleaned:
        names.append(cleaned)
  return tuple(names)


def clean_title(raw: str, artist: str = "") -> str:
  """Strip a title down to something a catalogue can match.

  Removes production noise, a redundant leading "Artist - " prefix, and any
  trailing feat. clause. The mix/remix designation is deliberately **kept**:
  it identifies a different recording.

  Args:
    raw: Title as downloaded.
    artist: Already-cleaned artist, used to strip a redundant prefix.

  Returns:
    The cleaned title.
  """
  text = _tidy(raw)
  text = _NOISE.sub(" ", text)
  text = _NOISE_BARE.sub("", text)

  text = _strip_artist_prefix(text, artist)
  text = _FEAT.sub(" ", text)
  return _tidy(text).strip(" -–:|")


def _strip_artist_prefix(text: str, artist: str) -> str:
  """Remove a redundant "Artist - " prefix, collaborators included.

  A plain exact-match strip fails on the common collaborator form: the title
  "Selena Gomez, Marshmello - Wolves" keeps its prefix because the cleaned
  artist is only "Selena Gomez". So the whole segment before the dash is
  removed when it *contains* the artist.

  Args:
    text: Title text.
    artist: Already-cleaned primary artist.

  Returns:
    The title without the redundant prefix.
  """
  if not artist or " - " not in text:
    return text
  head, _, tail = text.partition(" - ")
  if not tail.strip():
    return text
  # only strip when the head is a credit line, not part of the song's name
  if artist.casefold() in head.casefold() and len(head) < 60:
    return tail
  return text


def normalise(artist: str, title: str) -> Normalised:
  """Clean an artist/title pair for querying.

  Args:
    artist: Artist tag or channel name.
    title: Title as downloaded.

  Returns:
    A Normalised pair.
  """
  cleaned_artist = clean_artist(artist)
  full_artist = clean_artist_full(artist)
  featured = extract_featured(title)
  cleaned_title = clean_title(title, cleaned_artist)

  # "LMFAO - Party Rock Anthem" uploaded by a channel with no usable name:
  # fall back to splitting the title on the dash.
  if not cleaned_artist and " - " in cleaned_title:
    head, _, tail = cleaned_title.partition(" - ")
    if head.strip() and tail.strip():
      cleaned_artist, cleaned_title = _tidy(head), _tidy(tail)

  return Normalised(cleaned_artist, cleaned_title, featured, full_artist)
