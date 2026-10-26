"""Matches an inbound message to a configured command.

The router keeps an immutable snapshot of the enabled commands and is rebuilt
whenever `bot_config.config_version` changes, so portal edits take effect on
the next message without a restart.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from app.models import Command, MatchMode, TriggerType

log = logging.getLogger(__name__)

MAX_PATTERN_LENGTH = 512


@dataclass(slots=True)
class CompiledCommand:
    command_id: int
    name: str
    description: str
    trigger_type: str
    pattern: str
    match_mode: str
    case_sensitive: bool
    priority: int
    show_in_help: bool
    response_type: str
    static_response: str
    builtin_name: str | None
    integration_id: int | None
    script_name: str | None = None
    script_timeout_seconds: float = 30.0
    regex: re.Pattern[str] | None = None

    @classmethod
    def from_model(cls, command: Command) -> CompiledCommand | None:
        regex = None
        if command.trigger_type == TriggerType.regex.value:
            regex = compile_pattern(command.pattern, command.case_sensitive)
            if regex is None:
                log.warning(
                    "Command %r has an invalid regex and will be skipped", command.name
                )
                return None
        return cls(
            command_id=command.id,
            name=command.name,
            description=command.description or "",
            trigger_type=command.trigger_type,
            pattern=command.pattern or "",
            match_mode=command.match_mode,
            case_sensitive=command.case_sensitive,
            priority=command.priority,
            show_in_help=command.show_in_help,
            response_type=command.response_type,
            static_response=command.static_response or "",
            builtin_name=command.builtin_name,
            integration_id=command.integration_id,
            script_name=command.script_name,
            script_timeout_seconds=command.script_timeout_seconds or 30.0,
            regex=regex,
        )

    @property
    def help_usage(self) -> str:
        if self.trigger_type == TriggerType.keyword.value and (
            self.match_mode == MatchMode.prefix.value
        ):
            return f"{self.pattern} …"  # takes arguments
        return self.pattern


@dataclass(slots=True)
class MatchResult:
    command: CompiledCommand
    args_text: str = ""
    args: list[str] = field(default_factory=list)
    named: dict[str, str] = field(default_factory=dict)

    def template_vars(self) -> dict[str, Any]:
        return {
            "args": self.args_text,
            "argv": self.args,
            "groups": self.named,
            "command": {"name": self.command.name, "pattern": self.command.pattern},
        }


def compile_pattern(pattern: str, case_sensitive: bool) -> re.Pattern[str] | None:
    """Compile a user-supplied regex, refusing anything oversized."""
    if not pattern or len(pattern) > MAX_PATTERN_LENGTH:
        return None
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error:
        return None


def split_args(text: str) -> list[str]:
    """Split argument text shell-style, falling back to whitespace."""
    text = text.strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _fold(value: str, case_sensitive: bool) -> str:
    return value if case_sensitive else value.casefold()


class Router:
    """An immutable, priority-ordered view of the enabled commands."""

    def __init__(
        self, commands: list[CompiledCommand], *, config_version: int = 0
    ) -> None:
        # Lower priority number wins; ties broken by id for determinism.
        self.commands = sorted(commands, key=lambda c: (c.priority, c.command_id))
        self.config_version = config_version

    @classmethod
    def from_models(
        cls, models: list[Command], *, config_version: int = 0
    ) -> Router:
        compiled = [
            c
            for c in (CompiledCommand.from_model(m) for m in models if m.enabled)
            if c is not None
        ]
        return cls(compiled, config_version=config_version)

    def __len__(self) -> int:
        return len(self.commands)

    def help_entries(self) -> list[tuple[str, str]]:
        return [
            (c.help_usage, c.description)
            for c in self.commands
            if c.show_in_help
        ]

    def match(self, text: str) -> MatchResult | None:
        """Return the highest-priority command matching `text`, if any."""
        candidate = text.strip()
        if not candidate:
            return None
        for command in self.commands:
            result = _match_one(command, candidate)
            if result is not None:
                return result
        return None


def _match_one(command: CompiledCommand, text: str) -> MatchResult | None:
    if command.trigger_type == TriggerType.keyword.value:
        return _match_keyword(command, text)
    if command.trigger_type == TriggerType.regex.value:
        return _match_regex(command, text)
    return None


def _match_keyword(command: CompiledCommand, text: str) -> MatchResult | None:
    """A leading slash is tolerated on both sides, so `/help` == `help`."""
    body = text[1:] if text.startswith("/") else text
    folded_body = _fold(body.strip(), command.case_sensitive)
    folded_pattern = _fold(command.pattern.strip().lstrip("/"), command.case_sensitive)
    if not folded_pattern:
        return None

    if command.match_mode == MatchMode.exact.value:
        return _result(command, "") if folded_body == folded_pattern else None

    if command.match_mode == MatchMode.prefix.value:
        if folded_body == folded_pattern:
            return _result(command, "")
        size = len(folded_pattern)
        if folded_body.startswith(folded_pattern) and folded_body[size].isspace():
            return _result(command, body.strip()[size:])
        return None

    if command.match_mode == MatchMode.contains.value:
        return _result(command, body.strip()) if folded_pattern in folded_body else None

    return None


def _match_regex(command: CompiledCommand, text: str) -> MatchResult | None:
    if command.regex is None:
        return None
    match = command.regex.search(text)
    if match is None:
        return None
    named = {k: (v or "") for k, v in match.groupdict().items()}
    positional = [g or "" for g in match.groups() if g is not None]
    args_text = match.group(0)
    result = _result(command, args_text)
    result.named = named
    if positional and not named:
        result.args = positional
    return result


def _result(command: CompiledCommand, rest: str) -> MatchResult:
    args_text = rest.strip()
    return MatchResult(command=command, args_text=args_text, args=split_args(args_text))
