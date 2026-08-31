"""The context plane: typed episodes, priority eviction, validated compaction."""

from .episodes import Episode, EpisodeKind, TokenCounter, heuristic_tokens, PERMANENT, SALIENCE
from .window import (
    CompactionRefused,
    CompactionReport,
    ContextBudget,
    ContextWindow,
    counting_summarizer,
)

__all__ = [
    "Episode", "EpisodeKind", "TokenCounter", "heuristic_tokens", "PERMANENT", "SALIENCE",
    "CompactionRefused", "CompactionReport", "ContextBudget", "ContextWindow",
    "counting_summarizer",
]
