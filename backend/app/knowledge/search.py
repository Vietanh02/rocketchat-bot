"""Hybrid search: by meaning (vectors) and by words (SQLite FTS5), merged.

Vectors find texts that say the same thing in other words; word search
finds exact names, codes and numbers that vectors blur. The two ranked
lists are merged by reciprocal rank fusion (RRF), which needs no tuning
between their very different scores: an item's merged score is the sum of
1 / (K + rank) over the lists it appears in.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Iterable, Sequence

# The usual constant: high enough that the top rank of one list does not
# outweigh a strong showing in both.
RRF_K = 60
# Words of a question looked up; more add noise, not results.
MAX_QUERY_TERMS = 24

_WORD = re.compile(r"\w+", re.UNICODE)


def rrf(rankings: Iterable[Sequence[Hashable]], k: int = RRF_K) -> list[tuple[Hashable, float]]:
    """Merge ranked lists of ids, best first, as (id, score)."""
    scores: dict[Hashable, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def fts_query(text: str) -> str | None:
    """An FTS5 MATCH expression finding texts with any word of `text`.

    Each word is quoted, so nothing the user typed is read as FTS syntax
    (AND, NEAR, column filters, ...). None when there is no word to look for.
    """
    words: list[str] = []
    for word in _WORD.findall(text.casefold()):
        # One letter matches nearly everything; numbers are worth keeping.
        if (len(word) > 1 or word.isdigit()) and word not in words:
            words.append(word)
        if len(words) >= MAX_QUERY_TERMS:
            break
    if not words:
        return None
    return " OR ".join(f'"{word}"' for word in words)
