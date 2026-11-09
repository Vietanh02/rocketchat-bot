"""Decides what the bot says.

`Responder` is the seam for the bot's "brain". `RuleResponder` matches the
commands configured in the portal. In "llm" mode, a message that matches none
of them is answered by a language model (see `app.bot.llm`) instead of the
fallback reply. Integrations marked "LLM tool" are the model's tools. The
model's Markdown is rewritten for Rocket.Chat (`app.bot.rcformat`), both
while it streams in and once it is complete.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.intent import LOOKUP
from app.agent.queue import MERGED, QueueFull
from app.agent.runner import AgentResult, AgentRunner, AgentTurn
from app.agent.timing import TurnTimer
from app.agent.tools.integrations import IntegrationTools
from app.bot.builtins import BuiltinContext, get_builtin
from app.bot.config_store import ConfigSnapshot, ConfigStore
from app.bot.llm import LLMChat, LLMService
from app.bot.rcformat import strip_thinking, to_rocketchat
from app.bot.router import MatchResult
from app.bot.scripts import ScriptError, clip_output, result_to_reply, run_script
from app.bot.streaming import DeliveryOptions, LiveReply
from app.bot.types import MessageContext, Reply
from app.cache.answers import CachedAnswer
from app.cache.ttl import TTLCache
from app.integrations.executor import (
    IntegrationError,
    assert_url_allowed,
    bind_params,
    execute_integration,
)
from app.integrations.templating import TemplateRenderError, render
from app.knowledge.base import DocumentError
from app.knowledge.parsers import MAX_FILE_BYTES, is_supported
from app.models import ResponderMode, ResponseType
from app.rocketchat.ddp import RcFile
from app.rocketchat.gateway import Gateway
from app.rocketchat.rest import RocketChatError
from app.scheduler.service import SchedulerService

log = logging.getLogger(__name__)

GatewayProvider = Callable[[], Gateway | None]

# A gateway URL that passed the private-host check is trusted this long, so
# answering does not resolve the host's DNS on every message.
GATEWAY_CHECK_TTL_SECONDS = 300.0
# Documents read from one message; more are ignored.
MAX_DOCUMENTS_PER_MESSAGE = 5
# Answers a 👎 can still act on: how many, and for how long.
ANSWERED_KEPT = 2000
ANSWERED_TTL_SECONDS = 7 * 24 * 3600.0


@dataclass(slots=True)
class Answered:
    """A model answer posted in chat, as a later 👎 on it needs it."""

    message: MessageContext
    # Given from a kept answer.
    cached: CachedAnswer | None = None
    # Keeping it for next time (the id of the kept answer, when done).
    learning: asyncio.Task | None = None


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
        self.agent = AgentRunner(session_factory)
        # The document pipeline indexes with the configured model.
        self.agent.kb.model_access = self._kb_model
        self._gateway_checked: dict[str, float] = {}
        self._background: set[asyncio.Task] = set()
        # Posted message id -> what it answered.
        self._answered: TTLCache[Answered] = TTLCache(ANSWERED_KEPT, ANSWERED_TTL_SECONDS)

    async def _kb_model(self) -> tuple[ConfigSnapshot, LLMChat | None]:
        """The settings, and the model to index documents with when one is set up."""
        snapshot = await self._config_store.current()
        if self.llm is None or not snapshot.llm.configured:
            return snapshot, None
        return snapshot, self.llm.chat_for(snapshot.llm)

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
                    message,
                    snapshot,
                    live,
                    lambda lr: self._ask_llm(message, snapshot, lr, chat, post=live),
                    follow=True,
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
        *,
        follow: bool = False,
    ) -> Reply | None:
        """Run `answer`, showing its output as it arrives when streaming is on.

        `follow` shows the output as fast as it comes (a model's answer)
        instead of typing it out at the configured pace (a script's output).
        """
        live_reply = self._live_reply(message, snapshot, follow=follow) if live else None
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

    def goes_to_model(self, message: MessageContext, snapshot: ConfigSnapshot) -> bool:
        """Whether `message` would be answered by the model rather than a command."""
        return (
            snapshot.settings.enabled
            and self._llm_chat(snapshot) is not None
            and (message.is_direct or message.mentioned_bot)
            and self._should_consider(message, snapshot)
            and snapshot.access.allows(message.sender_username)
            and snapshot.router.match(message.clean_text or message.text) is None
        )

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
        self, message: MessageContext, snapshot: ConfigSnapshot, *, follow: bool = False
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
            follow=follow,
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
        *,
        post: bool = True,
    ) -> Reply | None:
        question = message.clean_text or message.text
        files = self._documents_in(message, snapshot)
        if not question.strip() and not files:
            return None
        timer = TurnTimer(started=message.arrived)
        # Show at once that the bot is on it; the answer takes seconds.
        ack = self._ack(message, snapshot, timer) if post else None

        intent: str | None = None
        problems: list[str] = []
        if files:
            # Documents sent with the message are read first, for this conversation.
            with timer.waiting("tools"):
                read, problems = await self._read_documents(message, snapshot, files)
            timer.labels["documents"] = str(len(read))
            if not question.strip():
                timer.labels["intent"] = "upload"
                lines = read + problems
                if read:
                    lines.append("Ask me anything about it. Send `new` to forget it.")
                error = None if read else "no document could be read"
                return self._model_reply(
                    message, "\n".join(lines), snapshot, timer, AgentResult("", "ok"), ack, error=error
                )
            if read:
                # A question sent with a document is about the document.
                intent = LOOKUP
        reply = await self._ask_model(message, snapshot, live, chat, question, timer, ack, intent)
        if problems and reply is not None:
            reply.text = "\n\n".join([reply.text, *problems])
        return reply

    def _documents_in(self, message: MessageContext, snapshot: ConfigSnapshot) -> list[RcFile]:
        """The files of `message` to read as documents, if reading them is on."""
        knowledge = snapshot.knowledge
        if not (knowledge.enabled and knowledge.chat_uploads):
            return []
        return [f for f in message.files if is_supported(f.name, f.type)][:MAX_DOCUMENTS_PER_MESSAGE]

    async def _read_documents(
        self, message: MessageContext, snapshot: ConfigSnapshot, files: list[RcFile]
    ) -> tuple[list[str], list[str]]:
        """Read the files into the conversation's documents: (what was read, what failed)."""
        gateway = self._gateway_provider()
        scope = self.agent.documents_scope(message, self._thread_for(message, snapshot))
        read: list[str] = []
        problems: list[str] = []
        for file in files:
            if gateway is None:
                problems.append(f":warning: Could not fetch *{file.name}*: not connected.")
                continue
            if file.size > MAX_FILE_BYTES:
                problems.append(
                    f":warning: *{file.name}* is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB."
                )
                continue
            try:
                data = await gateway.download_file(file.url, max_bytes=MAX_FILE_BYTES)
                info, created = await self.agent.kb.add(
                    scope,
                    file.name,
                    data,
                    content_type=file.type,
                    embedding=snapshot.embedding,
                    source="chat",
                    uploaded_by=message.sender_username,
                )
            except (RocketChatError, DocumentError) as exc:
                log.info("Could not read %s from %s: %s", file.name, message.message_id, exc)
                problems.append(f":warning: Could not read *{file.name}*: {exc}.")
                continue
            if not created:
                read.append(f":page_facing_up: I already have *{file.name}* in this conversation.")
                continue
            size = f"{info.pages} pages, " if info.pages else ""
            note = " (only the beginning: it is very long)" if info.truncated else ""
            read.append(
                f":page_facing_up: Read *{file.name}*: {size}{info.chunks} passages{note}."
            )
        return read, problems

    async def _ask_model(
        self,
        message: MessageContext,
        snapshot: ConfigSnapshot,
        live: LiveReply | None,
        chat: LLMChat,
        question: str,
        timer: TurnTimer,
        ack: asyncio.Task | None,
        intent: str | None,
    ) -> Reply | None:
        if not message.is_direct and message.sender_username:
            # Several people share a channel's conversation; say who is talking.
            question = f"@{message.sender_username}: {question}"
        try:
            # Same guard as integrations: no probing the host's private network.
            await self._check_gateway(snapshot.llm.base_url)
        except IntegrationError as exc:
            error = f"the language model gateway is not allowed: {exc}"
            log.warning("LLM reply refused: %s", error)
            return self._model_reply(
                message, f":warning: {error}", snapshot, timer, None, ack, error=error
            )

        tools = [
            IntegrationTools(
                (spec for spec in snapshot.integrations.values() if spec.llm_tool),
                message.template_vars(),
                cache=self.agent.tool_results,
            )
        ]
        if snapshot.llm.format_for_rocketchat:
            shape = to_rocketchat
        else:
            def shape(text: str, *, partial: bool = False) -> str:
                return strip_thinking(text, partial=partial)

        turn = AgentTurn(
            message,
            question,
            self._thread_for(message, snapshot),
            timer,
            live,
            intent=intent,
            fresh=message.fresh,
        )
        try:
            result = await self.agent.answer(
                turn, snapshot=snapshot, chat=chat, tools=tools, shape=shape
            )
        except QueueFull:
            error = "too many messages are waiting in this conversation; try again shortly"
            return self._model_reply(
                message, f":warning: {error}", snapshot, timer, None, ack, error=error
            )
        if live is not None and live.first_posted_at is not None:
            timer.mark_at("first_visible", live.first_posted_at)
        if result is MERGED:
            # Answered together with a later message.
            self._finish_turn(message, snapshot, timer, None, ack, sent=False)
            return None
        if result.status == "error":
            error = result.error or "the language model call failed"
            return self._model_reply(
                message, f":warning: {error}", snapshot, timer, result, ack, error=error
            )

        text = shape(result.text).strip()
        if result.status == "timeout":
            note = f":hourglass: _{result.error}; this is as far as I got._"
            if not text:
                error = f"the language model gave no answer before it was {result.error}"
                return self._model_reply(
                    message, f":warning: {error}", snapshot, timer, result, ack, error=error
                )
            return self._model_reply(message, f"{text}\n\n{note}", snapshot, timer, result, ack)
        if not text:
            error = "the language model returned an empty answer"
            return self._model_reply(
                message, f":warning: {error}", snapshot, timer, result, ack, error=error
            )
        return self._model_reply(message, text, snapshot, timer, result, ack)

    async def _check_gateway(self, base_url: str) -> None:
        now = time.monotonic()
        if self._gateway_checked.get(base_url, 0.0) > now:
            return
        await assert_url_allowed(base_url)
        self._gateway_checked = {base_url: now + GATEWAY_CHECK_TTL_SECONDS}

    def _ack(
        self, message: MessageContext, snapshot: ConfigSnapshot, timer: TurnTimer
    ) -> asyncio.Task | None:
        """Put the acknowledgement reaction on `message`, without waiting for it."""
        emoji = snapshot.agent.ack_reaction
        gateway = self._gateway_provider()
        if not emoji or gateway is None or not message.message_id:
            return None

        async def react() -> bool:
            try:
                await gateway.react(message.message_id, emoji)
            except Exception as exc:  # noqa: BLE001 - only a courtesy
                log.debug("Could not react to %s: %s", message.message_id, exc)
                return False
            timer.mark("ack")
            return True

        return self._spawn(react())

    def _spawn(self, coro: Awaitable) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _model_reply(
        self,
        message: MessageContext,
        text: str,
        snapshot: ConfigSnapshot,
        timer: TurnTimer,
        result: AgentResult | None,
        ack: asyncio.Task | None,
        *,
        error: str | None = None,
    ) -> Reply:
        reply = self._reply(message, text, None, snapshot, error=error)
        reply.timer = timer

        async def on_sent(sent: bool) -> None:
            if sent and reply.posted_id and result is not None and error is None:
                if result.cached is not None or result.learning is not None:
                    self._answered.put(
                        reply.posted_id, Answered(message, result.cached, result.learning)
                    )
            self._finish_turn(message, snapshot, timer, result, ack, sent=sent, error=error)

        reply.on_sent = on_sent
        return reply

    async def downvoted(self, message_id: str) -> MessageContext | None:
        """Someone reacted 👎 to the bot's message `message_id`.

        A kept answer is dropped (a pinned one only counted), and the
        question is returned to be answered again, afresh. An answer worked
        out by the model is not kept for next time.
        """
        answered = self._answered.pop(message_id)
        if answered is None:
            return None
        if answered.cached is not None:
            await self.agent.answers.downvote(answered.cached.entry_id)
            now = datetime.now(timezone.utc)
            return dataclasses.replace(
                answered.message, fresh=True, received_at=now, arrived=time.monotonic()
            )
        task = answered.learning
        if task is not None:
            try:
                entry_id = await asyncio.wait_for(asyncio.shield(task), 30)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                entry_id = None
            if entry_id is not None:
                await self.agent.answers.downvote(entry_id)
        return None

    def _finish_turn(
        self,
        message: MessageContext,
        snapshot: ConfigSnapshot,
        timer: TurnTimer,
        result: AgentResult | None,
        ack: asyncio.Task | None,
        *,
        sent: bool,
        error: str | None = None,
    ) -> None:
        """After the reply: take the reaction off and record the timings."""
        timer.finish()
        gateway = self._gateway_provider()
        emoji = snapshot.agent.ack_reaction

        async def wrap_up() -> None:
            if ack is not None and await ack and gateway is not None:
                try:
                    await gateway.react(message.message_id, emoji, on=False)
                except Exception as exc:  # noqa: BLE001 - only a courtesy
                    log.debug("Could not remove the reaction from %s: %s", message.message_id, exc)
            if result is None and error is None:
                await self.agent.record(timer, None, status="merged")
            else:
                await self.agent.record(
                    timer, result, error=error or (None if sent else "the reply could not be posted")
                )

        self._spawn(wrap_up())

    async def aclose(self) -> None:
        await self.agent.close()
        for task in list(self._background):
            task.cancel()

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
                    agent=self.agent,
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
                spec, params=params, base_context=context, cache=self.agent.tool_results
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
