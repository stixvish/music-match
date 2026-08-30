"""Matching a track against metadata sources, and scoring the result."""

from music_match.matching.matcher import Match
from music_match.matching.matcher import MatchStatus
from music_match.matching.matcher import SourceCandidate
from music_match.matching.matcher import match_track
from music_match.matching.score import Score
from music_match.matching.score import score_candidate

__all__ = [
    "Match",
    "MatchStatus",
    "Score",
    "SourceCandidate",
    "match_track",
    "score_candidate",
]
