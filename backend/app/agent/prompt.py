"""What the model is sent for one turn.

The order is chosen for the gateway's prompt cache: what changes least comes
first. The system prompt (with `{today}` at day granularity, never the time),
then the summary of folded-away messages, then the transcript, and the new
question last. Each turn's prompt therefore starts with the previous turn's,
and providers that cache prompt prefixes only process the new part.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from app.timeutil import in_timezone

Message = dict[str, str]

SUMMARY_HEADING = "Summary of the conversation so far (older messages are not shown):"
NOTES_HEADING = (
    "[Notes you saved in earlier conversations. They may be outdated; "
    "use them only if they help with this message.]"
)


def today_text(timezone_name: str | None, now: datetime | None = None) -> str:
    local = in_timezone(now or datetime.now(timezone.utc), timezone_name)
    assert local is not None
    return f"{local:%A, %Y-%m-%d} ({local.tzname() or timezone_name or 'UTC'})"


def system_text(
    template: str,
    *,
    bot_name: str,
    timezone_name: str | None,
    summary: str = "",
    profile: str = "",
) -> str:
    text = template.replace("{bot}", bot_name)
    if "{today}" in text:
        text = text.replace("{today}", today_text(timezone_name))
    if profile.strip():
        text = f"{text}\n\n{profile.strip()}"
    if summary.strip():
        text = f"{text}\n\n{SUMMARY_HEADING}\n{summary.strip()}"
    return text


def build_messages(
    template: str,
    *,
    bot_name: str,
    timezone_name: str | None,
    summary: str,
    history: list[Message],
    question: str,
    profile: str = "",
    notes: Sequence[tuple[int, str]] = (),
) -> list[Message]:
    """`notes` are (id, text) pairs recalled from memory for this question."""
    return [
        {
            "role": "system",
            "content": system_text(
                template,
                bot_name=bot_name,
                timezone_name=timezone_name,
                summary=summary,
                profile=profile,
            ),
        },
        *history,
        {"role": "user", "content": with_notes(question, notes)},
    ]


def with_notes(question: str, notes: Sequence[tuple[int, str]]) -> str:
    if not notes:
        return question
    lines = "\n".join(f"[#{note_id}] {text}" for note_id, text in notes)
    return f"{NOTES_HEADING}\n{lines}\n\n{question}"
