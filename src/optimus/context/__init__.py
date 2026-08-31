"""The context plane: typed episodes, priority eviction, validated compaction."""

from .episodes import PERMANENT, SALIENCE, Episode, EpisodeKind, TokenCounter, heuristic_tokens
from .window import (
    CompactionRefused,
    CompactionReport,
    ContextBudget,
    ContextWindow,
    counting_summarizer,
)

__all__ = [
    "PERMANENT",
    "SALIENCE",
    "CompactionRefused",
    "CompactionReport",
    "ContextBudget",
    "ContextWindow",
    "Episode",
    "EpisodeKind",
    "TokenCounter",
    "counting_summarizer",
    "heuristic_tokens",
]
