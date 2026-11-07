"""Decides what the bot says.

`Responder` is the seam for the bot's "brain". `RuleResponder` matches the
commands configured in the portal. In "llm" mode, a message that matches none
of them is answered by a language model (see `app.bot.llm`) instead of the
fallback reply. Integrations marked "LLM tool" are the model's tools. The
model's Markdown is rewritten for Rocket.Chat (`app.bot.rcformat`), both
while it streams in and once it is complete.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.builtins import BuiltinContext, get_builtin
from app.bot.config_store import ConfigSnapshot, ConfigStore
from app.bot.llm import LLMChat, LLMError, LLMService, Toolbox
from app.bot.rcformat import strip_thinking, to_rocketchat
from app.bot.router import MatchResult
from app.bot.scripts import ScriptError, clip_output, result_to_reply, run_script
from app.bot.streaming import DeliveryOptions, LiveReply
from app.bot.types import MessageContext, Reply
from app.integrations.executor import (
    IntegrationError,
    assert_url_allowed,
    bind_params,
    execute_integration,
)
from app.integrations.templating import TemplateRenderError, render
from app.models import ResponderMode, ResponseType
from app.rocketchat.gateway import Gateway
from app.scheduler.service import SchedulerService

log = logging.getLogger(__name__)

GatewayProvider = Callable[[], Gateway | None]


class Responder(Protocol):
    """Turns an inbound message into an optional reply."""

    async def respond(self, message: MessageContext, *, live: bool = True) -> Reply | None: ...


class RuleResponder:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config_store: ConfigStore,
        gateway_provider: GatewayProvider,
        scheduler: SchedulerService | None = None,
        llm: LLMService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config_store = config_store
        self._gateway_provider = gateway_provider
        self._scheduler = scheduler
        # Configured in the portal; "llm" mode falls back to rules until it is.
        self.llm = llm

    async def respond(self, message: MessageContext, *, live: bool = True) -> Reply | None:
        """`live=False` never posts while answering, as when the portal tests a command."""
        snapshot = await self._config_store.current()
        settings = snapshot.settings

        if not settings.enabled:
            return None
        if not self._should_consider(message, snapshot):
            return None
        if not snapshot.access.allows(message.sender_username):
            return self._denied(message, snapshot)

        match = snapshot.router.match(message.clean_text or message.text)
        if match is None:
            chat = self._llm_chat(snapshot)
            if chat is not None:
                # Like the fallback: never chime in uninvited in a channel.
                if not (message.is_direct or message.mentioned_bot):
                    return None
                return await self._with_live(
                    message, snapshot, live, lambda lr: self._ask_llm(message, snapshot, lr, chat)
                )
            return self._fallback(message, snapshot)

        # In streaming mode, a script's output is shown while it runs.
        live = live and match.command.response_type == ResponseType.script.value
        return await self._with_live(
            message, snapshot, live, lambda lr: self._answer(message, match, snapshot, lr)
        )

    async def _with_live(
        self,
        message: MessageContext,
        snapshot: ConfigSnapshot,
        live: bool,
        answer: Callable[[LiveReply | None], Awaitable[Reply | None]],
    ) -> Reply | None:
        """Run `answer`, showing its output as it arrives when streaming is on."""
        live_reply = self._live_reply(message, snapshot) if live else None
        try:
            reply = await answer(live_reply)
        except BaseException:
            if live_reply is not None:
                live_reply.abort()
            raise
        if live_reply is not None:
            if reply is None:
                live_reply.abort()
            else:
                if reply.error and live_reply.output:
                    # The user is watching the output; keep it above the warning.
                    reply.text = f"{live_reply.output}\n\n{reply.text}"
                reply.live = live_reply
        return reply

    async def _answer(
        self,
        message: MessageContext,
        match: MatchResult,
        snapshot: ConfigSnapshot,
        live: LiveReply | None,
    ) -> Reply | None:
        try:
            text = await self._render(message, match, snapshot, live)
        except (IntegrationError, ScriptError) as exc:
            log.info("Command %r failed: %s", match.command.name, exc)
            return self._reply(
                message,
                f":warning: {exc}",
                match.command.name,
                snapshot,
                error=str(exc),
            )
        except TemplateRenderError as exc:
            log.warning("Command %r has a broken template: %s", match.command.name, exc)
            return self._reply(
                message,
                f":warning: Command `{match.command.name}` has a broken template.",
                match.command.name,
                snapshot,
                error=str(exc),
            )

        if not text:
            return None
        return self._reply(message, text, match.command.name, snapshot)

    # ----------------------------------------------------------------- #

    def _llm_chat(self, snapshot: ConfigSnapshot) -> LLMChat | None:
        """The model to ask about unmatched messages, if "llm" mode is on."""
        if snapshot.settings.responder_mode != ResponderMode.llm.value or self.llm is None:
            return None
        return self.llm.chat_for(snapshot.llm)

    def _should_consider(
        self, message: MessageContext, snapshot: ConfigSnapshot
    ) -> bool:
        """In channels, stay quiet unless addressed."""
        if message.is_direct:
            return True
        if not snapshot.settings.respond_in_channels:
            return False
        text = (message.clean_text or "").strip()
        return message.mentioned_bot or text.startswith("/")

    def _fallback(
        self, message: MessageContext, snapshot: ConfigSnapshot
    ) -> Reply | None:
        fallback = snapshot.settings.fallback_reply.strip()
        if not fallback:
            return None
        # Never guess in a channel; only answer an unmatched DM or a direct mention.
        if not (message.is_direct or message.mentioned_bot):
            return None
        return self._reply(message, fallback, None, snapshot)

    def _denied(self, message: MessageContext, snapshot: ConfigSnapshot) -> Reply:
        """A refused user addressed the bot: log it, but never answer."""
        return Reply(text="", denied=True)

    def _live_reply(
        self, message: MessageContext, snapshot: ConfigSnapshot
    ) -> LiveReply | None:
        options = DeliveryOptions.from_settings(snapshot.settings)
        gateway = self._gateway_provider()
        if not options.streaming or gateway is None:
            return None
        return LiveReply(
            gateway,
            message.room_id,
            thread_id=self._thread_for(message, snapshot),
            options=options,
        )

    def _thread_for(self, message: MessageContext, snapshot: ConfigSnapshot) -> str | None:
        if message.thread_id is None and snapshot.settings.reply_in_thread:
            return message.message_id
        return message.thread_id

    def _reply(
        self,
        message: MessageContext,
        text: str,
        command_name: str | None,
        snapshot: ConfigSnapshot,
        *,
        error: str | None = None,
    ) -> Reply:
        return Reply(
            text=text,
            thread_id=self._thread_for(message, snapshot),
            command_name=command_name,
            error=error,
        )

    # ----------------------------------------------------------------- #

    async def _ask_llm(
        self,
        message: MessageContext,
        snapshot: ConfigSnapshot,
        live: LiveReply | None,
        chat: LLMChat,
    ) -> Reply | None:
        assert self.llm is not None
        question = message.clean_text or message.text
        if not question.strip():
            return None
        if not message.is_direct and message.sender_username:
            # Several people share a channel's conversation; say who is talking.
            question = f"@{message.sender_username}: {question}"
        try:
            # Same guard as integrations: no probing the host's private network.
            await assert_url_allowed(snapshot.llm.base_url)
        except IntegrationError as exc:
            error = f"the language model gateway is not allowed: {exc}"
            log.warning("LLM reply refused: %s", error)
            return self._reply(message, f":warning: {error}", None, snapshot, error=error)

        memory = self.llm.memory
        key = (message.room_id, self._thread_for(message, snapshot))
        messages = chat.build_messages(
            question,
            memory.history(key),
            bot_name=snapshot.settings.bot_username or "the bot",
        )
        tools = Toolbox(
            (spec for spec in snapshot.integrations.values() if spec.llm_tool),
            message.template_vars(),
        )
        if snapshot.llm.format_for_rocketchat:
            shape = to_rocketchat
        else:
            def shape(text: str, *, partial: bool = False) -> str:
                return strip_thinking(text, partial=partial)

        try:
            raw = await chat.complete(
                messages,
                on_text=None if live is None else lambda so_far: live.push(shape(so_far, partial=True)),
                tools=tools or None,
            )
        except LLMError as exc:
            log.info("LLM reply failed: %s", exc)
            return self._reply(message, f":warning: {exc}", None, snapshot, error=str(exc))
        text = shape(raw).strip()
        if not text:
            error = "the language model returned an empty answer"
            return self._reply(message, f":warning: {error}", None, snapshot, error=error)
        # The model's own words, so it keeps writing standard Markdown.
        memory.remember(key, question, strip_thinking(raw).strip())
        return self._reply(message, text, None, snapshot)

    async def _render(
        self,
        message: MessageContext,
        match: MatchResult,
        snapshot: ConfigSnapshot,
        live: LiveReply | None = None,
    ) -> str:
        command = match.command
        context = {**message.template_vars(), **match.template_vars()}

        if command.response_type == ResponseType.builtin.value:
            handler = get_builtin(command.builtin_name)
            if handler is None:
                raise IntegrationError(
                    f"built-in `{command.builtin_name}` does not exist"
                )
            return await handler(
                BuiltinContext(
                    message=message,
                    match=match,
                    snapshot=snapshot,
                    session_factory=self._session_factory,
                    gateway=self._gateway_provider(),
                    scheduler=self._scheduler,
                )
            )

        if command.response_type == ResponseType.integration.value:
            spec = snapshot.integrations.get(command.integration_id or -1)
            if spec is None:
                raise IntegrationError(
                    f"command `{command.name}` points at a missing or disabled "
                    "integration"
                )
            try:
                params = bind_params(spec.params, match.args, named=match.named)
            except IntegrationError:
                usage = f"/{command.pattern} {spec.usage()}".strip()
                raise IntegrationError(f"Usage: `{usage}`") from None
            result = await execute_integration(
                spec, params=params, base_context=context
            )
            return result.text

        if command.response_type == ResponseType.script.value:
            if not command.script_name:
                raise ScriptError(f"command `{command.name}` has no script selected")
            result = await run_script(
                command.script_name,
                args=match.args,
                context=script_context(message, match),
                timeout=command.script_timeout_seconds,
                on_output=None if live is None else lambda out: live.push(clip_output(out)),
            )
            log.info(
                "Script %r exited %s in %dms",
                command.script_name,
                "on timeout" if result.timed_out else result.exit_code,
                result.duration_ms,
            )
            return result_to_reply(
                command.script_name, result, command.script_timeout_seconds
            )

        return render(command.static_response, context)


def script_context(message: MessageContext, match: MatchResult) -> dict:
    """What a script receives on stdin (and, flattened, as BOT_* variables)."""
    variables = message.template_vars()
    return {
        "command": match.command.name,
        "args": match.args,
        "args_text": match.args_text,
        "groups": match.named,
        "user": variables["user"],
        "room": variables["room"],
        "message": variables["message"],
        "now": message.received_at.isoformat(),
    }
