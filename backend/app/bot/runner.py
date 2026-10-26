"""The bot service: owns the Rocket.Chat connection and the message loop.

Inbound messages arrive on a DDP stream and are queued, so a slow handler
never stalls the socket reader. A dispatcher then sorts them into lanes, one
per (room, sender): each lane is handled in order, and up to
`APP_BOT_MAX_CONCURRENCY` lanes run at once. One user's slow script therefore
delays only that user's next message, and one user can never take more than
one slot. Every handled message is written to `message_log` for the portal's
Logs page.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict, deque
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.config_store import BotSettingsSnapshot, ConfigStore
from app.bot.responder import Responder, RuleResponder
from app.bot.streaming import DeliveryOptions, deliver
from app.bot.types import MessageContext, Reply
from app.config import get_settings
from app.models import MessageLog
from app.rocketchat.ddp import DDPClient, IncomingMessage
from app.rocketchat.gateway import ConnectionStatus, Gateway, LiveGateway
from app.rocketchat.rest import RocketChatError, RocketChatRest
from app.scheduler.service import SchedulerService

log = logging.getLogger(__name__)

SEEN_CACHE_SIZE = 512
# Messages one user may have waiting in a room; more are dropped as spam.
MAX_LANE_BACKLOG = 20

LaneKey = tuple[str, str]


class BotService:
    """Starts, stops and restarts the bot's link to Rocket.Chat."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config_store: ConfigStore,
        scheduler: SchedulerService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config_store = config_store
        self._scheduler = scheduler
        self._settings = get_settings()

        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue(
            maxsize=self._settings.inbound_queue_size
        )
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lanes: dict[LaneKey, deque[IncomingMessage]] = {}
        self._lane_tasks: set[asyncio.Task] = set()
        self._slots = asyncio.Semaphore(self._settings.bot_max_concurrency)
        self._active = 0

        self._gateway: Gateway | None = None
        self._rest: RocketChatRest | None = None
        self._ddp: DDPClient | None = None
        self._tasks: list[asyncio.Task] = []
        self._bot_user_id: str | None = None
        self._bot_username: str | None = None
        self._start_lock = asyncio.Lock()
        self._last_error: str | None = None
        self._configured = False
        self._handled = 0

        self.responder: Responder = RuleResponder(
            session_factory, config_store, self.gateway, scheduler
        )

    # ----------------------------------------------------------------- #
    # Accessors
    # ----------------------------------------------------------------- #

    def gateway(self) -> Gateway | None:
        """Provider form, so the responder always sees the current gateway."""
        return self._gateway

    @property
    def status(self) -> ConnectionStatus:
        return ConnectionStatus(
            configured=self._configured,
            connected=bool(self._ddp and self._ddp.is_connected),
            bot_user_id=self._bot_user_id,
            bot_username=self._bot_username,
            rest_url=self._rest.base_url if self._rest else None,
            ws_url=self._ddp.ws_url if self._ddp else None,
            last_error=self._last_error or (self._ddp.last_error if self._ddp else None),
            messages_handled=self._handled,
            details={
                "queue_depth": str(self._queue.qsize()),
                "active_handlers": str(self._active),
                "max_concurrency": str(self._settings.bot_max_concurrency),
                # Queued in a lane, plus lanes whose next message awaits a slot.
                "waiting": str(
                    sum(len(lane) for lane in self._lanes.values())
                    + max(0, len(self._lanes) - self._active)
                ),
            },
        )

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #

    async def start(self) -> None:
        """Connect using the stored configuration. Safe to call repeatedly."""
        async with self._start_lock:
            await self._stop_locked()
            snapshot = await self._config_store.reload()
            settings = snapshot.settings

            if not settings.enabled:
                self._last_error = "bot is disabled in the portal"
                log.info("Bot is disabled; not connecting")
                return
            if not settings.rc_rest_url or not settings.rc_ws_url:
                self._last_error = "Rocket.Chat URLs are not configured"
                log.warning("%s", self._last_error)
                return
            if not settings.has_credentials:
                self._last_error = (
                    "no bot credentials configured (set a personal access token "
                    "or a username and password in the portal)"
                )
                log.warning("%s", self._last_error)
                return

            try:
                await self._connect(settings)
            except (RocketChatError, ValueError) as exc:
                self._last_error = str(exc)
                log.error("Bot could not start: %s", exc)
                await self._stop_locked()
                return

            self._configured = True
            self._last_error = None
            self._tasks = [
                asyncio.create_task(self._ddp.run_forever(), name="ddp"),  # type: ignore[union-attr]
                asyncio.create_task(self._dispatch(), name="bot-dispatcher"),
            ]
            log.info("Bot started as @%s", self._bot_username or "?")

    async def _connect(self, settings: BotSettingsSnapshot) -> None:
        rest = RocketChatRest(
            settings.rc_rest_url,
            user_id=settings.bot_user_id,
            auth_token=settings.auth_token,
        )
        token = settings.auth_token

        if not token:
            # Username/password path: trade them for a token once, up front.
            _, token = await rest.login(settings.bot_username or "", settings.password or "")

        identity = await rest.me()
        self._bot_user_id = identity.get("_id") or settings.bot_user_id
        self._bot_username = identity.get("username") or settings.bot_username
        rest.user_id = self._bot_user_id
        rest.auth_token = token

        self._rest = rest
        self._gateway = LiveGateway(rest)
        if self._scheduler is not None:
            self._scheduler.set_gateway(self._gateway)

        self._ddp = DDPClient(
            settings.rc_ws_url,
            auth_token=token,
            username=settings.bot_username,
            password=settings.password,
            on_message=self._enqueue,
        )

    async def stop(self) -> None:
        async with self._start_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        if self._ddp is not None:
            await self._ddp.stop()
        tasks = [*self._tasks, *self._lane_tasks]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        self._lane_tasks.clear()
        self._lanes.clear()
        if self._rest is not None:
            await self._rest.aclose()
        self._ddp = None
        self._rest = None
        self._gateway = None
        if self._scheduler is not None:
            self._scheduler.set_gateway(None)
        self._configured = False

    async def restart(self) -> None:
        """Called after the portal changes bot settings."""
        self._config_store.invalidate()
        await self.start()

    # ----------------------------------------------------------------- #
    # Message pipeline
    # ----------------------------------------------------------------- #

    async def _enqueue(self, message: IncomingMessage) -> None:
        if not self._is_actionable(message):
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            log.warning("Inbound queue is full; dropping message %s", message.message_id)

    def _is_actionable(self, message: IncomingMessage) -> bool:
        if message.is_system or message.is_edit or not message.text.strip():
            return False
        # Never answer ourselves, and never answer another bot (loop risk).
        if message.sender_id and message.sender_id == self._bot_user_id:
            return False
        if message.is_bot:
            return False
        if message.message_id in self._seen:
            return False
        self._seen[message.message_id] = None
        while len(self._seen) > SEEN_CACHE_SIZE:
            self._seen.popitem(last=False)
        return True

    async def _dispatch(self) -> None:
        """Move queued messages into their lanes, starting a lane if idle."""
        while True:
            message = await self._queue.get()
            try:
                self._route(message)
            finally:
                self._queue.task_done()

    def _route(self, message: IncomingMessage) -> None:
        key: LaneKey = (message.room_id, message.sender_id or message.sender_username or "")
        lane = self._lanes.get(key)
        if lane is not None:
            if len(lane) >= MAX_LANE_BACKLOG:
                log.warning(
                    "@%s has %d messages waiting in %s; dropping %s",
                    message.sender_username, len(lane), message.room_id, message.message_id,
                )
                return
            lane.append(message)
            return
        self._lanes[key] = deque([message])
        task = asyncio.create_task(self._run_lane(key), name=f"bot-lane-{key[0]}-{key[1]}")
        self._lane_tasks.add(task)
        task.add_done_callback(self._lane_tasks.discard)

    async def _run_lane(self, key: LaneKey) -> None:
        """Handle one (room, sender)'s messages in order, then retire the lane."""
        lane = self._lanes.get(key)
        if lane is None:  # the bot was stopped before this lane began
            return
        try:
            while lane:
                message = lane.popleft()
                async with self._slots:
                    self._active += 1
                    try:
                        await self._handle(message)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - one bad message must not kill the lane
                        log.exception("Unhandled error while processing %s", message.message_id)
                    finally:
                        self._active -= 1
        finally:
            # No await between the empty check and here, so nothing can slip
            # into the lane unseen.
            if self._lanes.get(key) is lane:
                del self._lanes[key]
            self._lane_tasks.discard(asyncio.current_task())  # type: ignore[arg-type]

    async def drain(self) -> None:
        """Wait until every queued and in-flight message is handled."""
        while True:
            await self._queue.join()
            pending = [task for task in self._lane_tasks if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def _handle(self, message: IncomingMessage) -> None:
        started = datetime.now(timezone.utc)
        context = MessageContext.from_incoming(
            message, bot_username=self._bot_username
        )

        reply: Reply | None = None
        status, error = "ignored", None
        try:
            reply = await self.responder.respond(context)
        except Exception as exc:  # noqa: BLE001 - logged, and the user is told
            status, error = "error", str(exc)
            log.exception("Responder failed for %s", message.message_id)
            reply = Reply(text=":warning: Something went wrong handling that.")

        if reply is not None and reply.denied:
            status = "denied"
        elif reply is not None and reply.text:
            try:
                await self._send(context, reply)
                self._handled += 1
                status = "error" if reply.error else "ok"
                error = error or reply.error
            except RocketChatError as exc:
                status, error = "error", str(exc)
                log.error("Could not send reply to %s: %s", context.room_id, exc)

        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        await self._log(context, reply, status, error, duration_ms)

    async def _send(self, context: MessageContext, reply: Reply) -> None:
        if self._gateway is None:
            raise RocketChatError("not connected to Rocket.Chat")
        snapshot = await self._config_store.current()
        await deliver(
            self._gateway,
            reply.room_id or context.room_id,
            reply.text,
            thread_id=reply.thread_id,
            options=DeliveryOptions.from_settings(snapshot.settings),
        )

    async def _log(
        self,
        context: MessageContext,
        reply: Reply | None,
        status: str,
        error: str | None,
        duration_ms: int,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                MessageLog(
                    received_at=context.received_at,
                    room_id=context.room_id,
                    room_type=context.room_type,
                    sender_id=context.sender_id,
                    sender_username=context.sender_username,
                    text_in=context.text[:4000],
                    matched_command=reply.command_name if reply else None,
                    text_out=(reply.text if reply else "")[:4000],
                    status=status,
                    error=(error or None) and error[:2000],
                    duration_ms=duration_ms,
                )
            )
            await session.commit()
