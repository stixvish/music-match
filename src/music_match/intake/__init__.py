"""Intake: link parsing, the pre-download dedup layers, and downloading.

The download function is `download_entry` rather than `download`, which
would shadow the `intake.download` submodule of the same name.
"""

from music_match.intake.dedup import Candidate
from music_match.intake.dedup import find_candidates
from music_match.intake.dedup import in_archive
from music_match.intake.dedup import record_download
from music_match.intake.download import Download
from music_match.intake.download import download_entry
from music_match.intake.entries import Entry
from music_match.intake.entries import IntakeError
from music_match.intake.entries import expand
from music_match.intake.entries import parse_links

__all__ = [
    "Candidate",
    "Download",
    "Entry",
    "IntakeError",
    "download_entry",
    "expand",
    "find_candidates",
    "in_archive",
    "parse_links",
    "record_download",
]
