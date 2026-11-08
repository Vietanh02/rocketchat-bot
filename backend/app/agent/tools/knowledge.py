"""`kb_search`: the model searching the documents itself.

Passages that match a question are already shown with it; this is for
looking further, or in the other set of documents. Like memory, the scopes
are fixed by the conversation: its own documents and the knowledge base.
"""

from __future__ import annotations

from typing import Any

from app.agent.tools import tool_result
from app.bot.config_store import EmbeddingSettings
from app.knowledge.base import BASE_SCOPE, KnowledgeBase

SEARCH_LIMIT = 8
PASSAGE_CHARS = 1200


class KnowledgeTools:
    def __init__(
        self,
        kb: KnowledgeBase,
        conversation: str,
        embedding: EmbeddingSettings,
        *,
        available: set[str],
        min_score: float | None = None,
    ) -> None:
        self._kb = kb
        self._conversation = conversation
        self._embedding = embedding
        self._available = available
        self._min_score = min_score

    def tools(self) -> dict[str, dict[str, Any]]:
        where = []
        if self._conversation in self._available:
            where.append("conversation")
        if BASE_SCOPE in self._available:
            where.append("knowledge_base")
        return {
            "kb_search": {
                "description": (
                    "Search the documents for passages about something: the knowledge base "
                    "set up by the admins, and documents sent in this conversation. Use it for "
                    "questions about policies, procedures, products or anything the documents "
                    "may cover. Cite what you use with the passage's label."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to look for."},
                        "where": {
                            "type": "string",
                            "enum": [*where, "all"],
                            "description": "Which documents; default all.",
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_LIMIT},
                    },
                    "required": ["query"],
                },
            }
        }

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return tool_result({"error": "query must be a non-empty string"})
        where = arguments.get("where") or "all"
        scopes = {
            "conversation": [self._conversation],
            "knowledge_base": [BASE_SCOPE],
        }.get(where, [self._conversation, BASE_SCOPE])
        scopes = [scope for scope in scopes if scope in self._available]
        if not scopes:
            return tool_result({"passages": [], "note": f"there are no documents in {where}"})
        try:
            limit = max(1, min(SEARCH_LIMIT, int(arguments.get("limit") or 5)))
        except (TypeError, ValueError):
            limit = 5
        passages = await self._kb.search(
            query, scopes, embedding=self._embedding, limit=limit, min_score=self._min_score
        )
        return tool_result(
            {
                "passages": [
                    {
                        "label": passage.label,
                        "from": "conversation" if passage.scope != BASE_SCOPE else "knowledge_base",
                        "text": passage.text[:PASSAGE_CHARS],
                    }
                    for passage in passages
                ]
            }
        )
