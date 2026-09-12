"""Local genre classification for precedence routing (SPEC.md §7).

**This is a router, not a genre source.** Its `Style` output is not trustworthy
— it splits near-identical tracks across `Trap` / `Cloud Rap` / `Pop Rap` on
uncalibrated activations — so `Style` is never written as a tag. Only the
top-level genre is used, and only to choose which precedence table applies.

Running locally also breaks a circular dependency: precedence is per-genre, but
genre is itself one of the contested fields. A source-independent classifier
settles it before any API call.
"""

import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_BASE = "https://essentia.upf.edu/models"
MODEL_DIR = Path.home() / ".cache" / "musicpipeline" / "models"

MODELS = {
  "discogs-effnet-bs64-1.pb": "feature-extractors/discogs-effnet",
  "genre_discogs400-discogs-effnet-1.pb": "classification-heads/genre_discogs400",
  "genre_discogs400-discogs-effnet-1.json": "classification-heads/genre_discogs400",
}

# the six families precedence is keyed on (SPEC.md §7).
FAMILIES = ("electronic", "hip-hop", "pop", "r&b-soul", "world", "other")

# discogs top-level genre -> family. rock and pop share a family because they
# would use an identical precedence table; splitting them buys nothing.
_FAMILY_MAP = {
  "electronic": "electronic",
  "hip hop": "hip-hop",
  "hip-hop": "hip-hop",
  "pop": "pop",
  "rock": "pop",
  "funk / soul": "r&b-soul",
  "funk/soul": "r&b-soul",
  "latin": "world",
  "reggae": "world",
  "folk, world, & country": "world",
  "folk world & country": "world",
  # bollywood is filed inconsistently by discogs — sometimes under folk/world,
  # sometimes stage & screen. both route to `world` so the ~105 bollywood
  # tracks land on one precedence table either way (SPEC.md §7 risk note).
  "stage & screen": "world",
}


@dataclass(frozen=True)
class Prediction:
  """A classifier result. `style` is diagnostic only and never tagged."""

  label: str
  activation: float

  @property
  def genre(self) -> str:
    """The top-level Discogs genre."""
    return self.label.split("---")[0] if self.label else ""

  @property
  def style(self) -> str:
    """The sub-genre. Diagnostic only — unreliable, never written as a tag."""
    parts = self.label.split("---")
    return parts[1] if len(parts) > 1 else ""

  @property
  def family(self) -> str:
    """The precedence family this track routes to."""
    return family_for(self.genre)


def family_for(genre: str) -> str:
  """Map a Discogs top-level genre to a precedence family.

  Args:
    genre: A Discogs top-level genre, e.g. "Hip Hop".

  Returns:
    One of `FAMILIES`; anything unrecognised becomes `other`.
  """
  return _FAMILY_MAP.get((genre or "").strip().casefold(), "other")


# ISRC registrant-country prefixes whose music the discogs-400 model cannot
# classify, because it has no class for it. cp5 measured bollywood scattering
# across pop/world/electronic with predicted styles of "K-pop", "Laïkó" and
# "Pachanga" — nearest-neighbour guesses, not categories.
# Catalogue genres that name a regional tradition directly. iTunes tagged 46
# of 49 Bollywood tracks correctly where the classifier got 0 (SPEC.md §7), and
# an ISRC only catches recordings registered in the region — a Bollywood track
# released under a UK ISRC needs this instead.
REGIONAL_GENRES = frozenset(
  {
    "bollywood",
    "indian pop",
    "telugu",
    "tamil",
    "punjabi",
    "bhangra",
    "filmi",
    "desi pop",
    "hindustani",
    "carnatic",
    "qawwali",
    "ghazal",
  }
)

# Indian film labels. Measured on the real library: T-Series, Tips, Eros
# International and Eros Music appeared on 19 Bollywood tracks and **zero**
# non-Bollywood ones. Global labels like Sony Music are deliberately absent —
# they carry everything.
REGIONAL_LABELS = frozenset(
  {
    "t-series",
    "tips",
    "tips industries",
    "eros international",
    "eros music",
    "zee music company",
    "saregama",
    "venus records",
    "yrf music",
    "t series",
  }
)

REGIONAL_ISRC_PREFIXES = {
  "IN": "world",  # india
  "LK": "world",  # sri lanka
  "PK": "world",  # pakistan
  "BD": "world",  # bangladesh
}


def family_for_identity(
  genre: str,
  isrc: str | None = None,
  catalogue_genre: str = "",
  label: str = "",
) -> str:
  """Choose a precedence family, letting ISRC country override the classifier.

  The classifier is the default, but it has no class for several regional
  traditions and guesses a nearest neighbour instead. An ISRC registrant
  country is a cheap, reliable signal for exactly those cases.

  This does **not** reintroduce the circular dependency the classifier exists
  to break: ISRC comes from identity resolution, not from one of the contested
  metadata fields being arbitrated.

  Args:
    genre: Top-level genre from the classifier.
    isrc: The recording's ISRC, once known.
    catalogue_genre: A catalogue's own genre, which names regional traditions
      the classifier has no class for.
    label: The release label; the Indian film labels are unambiguous.

  Returns:
    One of `FAMILIES`.
  """
  if isrc and len(isrc) >= 2:
    override = REGIONAL_ISRC_PREFIXES.get(isrc[:2].upper())
    if override:
      return override
  if catalogue_genre.strip().casefold() in REGIONAL_GENRES:
    return "world"
  if _normalise_label(label) in REGIONAL_LABELS:
    return "world"
  return family_for(genre)


def _normalise_label(label: str) -> str:
  return " ".join((label or "").casefold().replace(".", " ").split())


def model_paths() -> dict[str, Path]:
  """Where each model file lives on disk.

  Returns:
    Filename to path.
  """
  return {name: MODEL_DIR / name for name in MODELS}


def models_present() -> bool:
  """Whether every model file has been downloaded.

  Returns:
    True if all files exist.
  """
  return all(path.exists() for path in model_paths().values())


def fetch_models() -> list[Path]:
  """Download the classifier models if they are missing (~20 MB, once).

  Returns:
    Paths to all model files.
  """
  MODEL_DIR.mkdir(parents=True, exist_ok=True)
  written: list[Path] = []
  for name, prefix in MODELS.items():
    dest = MODEL_DIR / name
    if not dest.exists():
      log.info("fetching %s", name)
      urllib.request.urlretrieve(f"{MODEL_BASE}/{prefix}/{name}", dest)  # noqa: S310
    written.append(dest)
  return written


class Classifier:
  """Essentia genre classifier, loaded lazily.

  Loading pulls in TensorFlow and ~20 MB of graphs, so it is deferred until the
  first call rather than paid at import.
  """

  def __init__(self) -> None:
    """Create a classifier without loading anything yet."""
    self._embeddings = None
    self._head = None
    self._labels: list[str] = []

  def _load(self) -> None:
    if self._head is not None:
      return
    import json

    import essentia.standard as es

    paths = model_paths()
    if not models_present():
      fetch_models()
    self._labels = json.loads(
      paths["genre_discogs400-discogs-effnet-1.json"].read_text()
    )["classes"]
    self._embeddings = es.TensorflowPredictEffnetDiscogs(
      graphFilename=str(paths["discogs-effnet-bs64-1.pb"]),
      output="PartitionedCall:1",
    )
    self._head = es.TensorflowPredict2D(
      graphFilename=str(paths["genre_discogs400-discogs-effnet-1.pb"]),
      input="serving_default_model_Placeholder",
      output="PartitionedCall:0",
    )

  def predict(self, path: Path, top: int = 3) -> list[Prediction]:
    """Classify an audio file.

    Args:
      path: Audio file, any format ffmpeg can read.
      top: How many predictions to return.

    Returns:
      Predictions, strongest first.
    """
    self._load()
    import essentia.standard as es
    import numpy as np

    # the model expects 16 kHz mono
    audio = es.MonoLoader(filename=str(path), sampleRate=16000, resampleQuality=4)()
    assert self._embeddings is not None
    assert self._head is not None
    activations = self._head(self._embeddings(audio)).mean(axis=0)
    order = np.argsort(activations)[::-1][:top]
    return [
      Prediction(label=self._labels[i], activation=float(activations[i])) for i in order
    ]

  def family(self, path: Path) -> str:
    """Classify a file and return only its precedence family.

    Args:
      path: Audio file.

    Returns:
      One of `FAMILIES`.
    """
    predictions = self.predict(path, top=1)
    return predictions[0].family if predictions else "other"
