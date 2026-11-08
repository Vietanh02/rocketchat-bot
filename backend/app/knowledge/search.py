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

# Words that say how a question is asked, not what it is about. Searching
# for them finds nearly every passage. Vietnamese syllables are separate words.
STOPWORDS = frozenset(
    """
    là của và có không cho các những được trong một này đó kia thì mà với để từ khi như
    về ở tôi bạn mình em anh chị ông bà gì nào sao thế nhé ạ à ơi hãy giúp làm ơn vậy
    bao nhiêu đâu ai cái chiếc việc nên phải cần muốn biết hỏi xin vui lòng thể theo
    đã đang sẽ rồi lại cũng chỉ rất quá hay hoặc nếu nhưng vì bởi tại ra vào lên xuống
    the a an is are was were be been what how why when where who whom which whose do does did
    of to in on at for and or but if i you me my your we our it its this that these those
    please can could would should will tell about with from by as into than then there
    """.split()
)


def rrf(rankings: Iterable[Sequence[Hashable]], k: int = RRF_K) -> list[tuple[Hashable, float]]:
    """Merge ranked lists of ids, best first, as (id, score)."""
    scores: dict[Hashable, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def fts_query(text: str, *, skip_stopwords: bool = False) -> str | None:
    """An FTS5 MATCH expression finding texts with any word of `text`.

    Each word is quoted, so nothing the user typed is read as FTS syntax
    (AND, NEAR, column filters, ...). None when there is no word to look for.
    With `skip_stopwords`, words like "là" and "what" are left out, and a
    text of nothing else looks for nothing.
    """
    found = _WORD.findall(text.casefold())
    if skip_stopwords:
        found = [word for word in found if word not in STOPWORDS]
    words: list[str] = []
    for word in found:
        # One letter matches nearly everything; numbers are worth keeping.
        if (len(word) > 1 or word.isdigit()) and word not in words:
            words.append(word)
        if len(words) >= MAX_QUERY_TERMS:
            break
    if not words:
        return None
    return " OR ".join(f'"{word}"' for word in words)
