"""Memory and profile tools offered to the model.

Every call acts on the scopes of the conversation it is made in, fixed when
the tools are built for the turn; no argument can reach another scope.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent.memory import MemoryScopes, MemoryStore, NoteError
from app.agent.profiles import FIELDS, ProfileError, ProfileStore
from app.agent.tools import tool_result
from app.bot.config_store import EmbeddingSettings

log = logging.getLogger(__name__)

SEARCH_LIMIT = 10


class MemoryTools:
    def __init__(
        self,
        memory: MemoryStore,
        scopes: MemoryScopes,
        embedding: EmbeddingSettings,
        *,
        actor: str | None,
        session_id: int | None = None,
        in_channel: bool = False,
    ) -> None:
        self._memory = memory
        self._scopes = scopes
        self._embedding = embedding
        self._actor = actor
        self._session_id = session_id
        where = "this channel" if in_channel else "this user"
        self._schemas = {
            "memory_save": {
                "description": (
                    f"Remember a fact for later conversations with {where}: a preference, "
                    "a decision, a name, something they asked you to remember. Write one "
                    "short, self-contained statement that will still make sense on its own "
                    "later (include who and when if it matters). Do not save small talk, "
                    "secrets such as passwords, or what is only useful right now."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"note": {"type": "string", "description": "The fact to keep."}},
                    "required": ["note"],
                },
            },
            "memory_search": {
                "description": (
                    "Search what you remembered from earlier conversations. Notes that look "
                    "relevant are already shown with the question; search for anything else."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to look for."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_LIMIT},
                    },
                    "required": ["query"],
                },
            },
            "memory_forget": {
                "description": (
                    "Forget a remembered note that is wrong, outdated, or that the user asks "
                    "you to forget. Takes the note's number, as in [#12]."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "integer", "description": "The note's number."}},
                    "required": ["id"],
                },
            },
        }

    def tools(self) -> dict[str, dict[str, Any]]:
        return self._schemas

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "memory_save":
            note = arguments.get("note")
            if not isinstance(note, str):
                return tool_result({"error": "note must be a string"})
            try:
                saved, created = await self._memory.add(
                    self._scopes.write,
                    note,
                    embedding=self._embedding,
                    source="tool",
                    created_by=self._actor,
                    session_id=self._session_id,
                )
            except NoteError as exc:
                return tool_result({"error": str(exc)})
            if not created:
                return tool_result({"ok": True, "id": saved.id, "note": "already remembered"})
            log.info("Remembered note %d in %s", saved.id, self._scopes.write)
            return tool_result({"ok": True, "id": saved.id})

        if name == "memory_search":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                return tool_result({"error": "query must be a non-empty string"})
            try:
                limit = max(1, min(SEARCH_LIMIT, int(arguments.get("limit") or 5)))
            except (TypeError, ValueError):
                limit = 5
            notes = await self._memory.search(
                query, self._scopes.read, embedding=self._embedding, limit=limit
            )
            return tool_result(
                {
                    "notes": [
                        {
                            "id": note.id,
                            "note": note.text,
                            "saved": note.created_at.date().isoformat() if note.created_at else None,
                        }
                        for note in notes
                    ]
                }
            )

        if name == "memory_forget":
            try:
                note_id = int(arguments.get("id"))
            except (TypeError, ValueError):
                return tool_result({"error": "id must be a note number"})
            # Global notes are read here but only the portal removes them.
            gone = await self._memory.delete(note_id, [self._scopes.write])
            if gone is None:
                return tool_result({"error": f"there is no note #{note_id} you can forget"})
            log.info("Forgot note %d of %s", note_id, self._scopes.write)
            return tool_result({"ok": True})

        return tool_result({"error": f"unknown tool {name!r}"})


class ProfileTools:
    """`profile_update`, for the person in a DM to change their own profile."""

    def __init__(self, profiles: ProfileStore, rc_user_id: str, username: str) -> None:
        self._profiles = profiles
        self._rc_user_id = rc_user_id
        self._username = username

    def tools(self) -> dict[str, dict[str, Any]]:
        return {
            "profile_update": {
                "description": (
                    "Update the profile of the user you are talking with, when they tell you "
                    "how to call them, which language or timezone to use, or something about "
                    "themselves to keep in mind. Only pass the fields that change; an empty "
                    "string clears a field."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "display_name": {"type": "string"},
                        "language": {"type": "string", "description": "e.g. Vietnamese"},
                        "timezone": {"type": "string", "description": "e.g. Asia/Ho_Chi_Minh"},
                        "about": {
                            "type": "string",
                            "description": "Role, team, how they like answers; replaces the old text.",
                        },
                    },
                },
            }
        }

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        fields = {key: value for key, value in arguments.items() if key in FIELDS}
        if not fields:
            return tool_result({"error": f"pass at least one of: {', '.join(FIELDS)}"})
        try:
            profile = await self._profiles.update(self._rc_user_id, self._username, **fields)
        except ProfileError as exc:
            return tool_result({"error": str(exc)})
        log.info("Updated the profile of @%s: %s", self._username, ", ".join(sorted(fields)))
        return tool_result({"ok": True, "profile": {field: getattr(profile, field) for field in FIELDS}})
