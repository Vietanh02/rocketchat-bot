"""FastAPI application: the portal API plus the bot and scheduler lifecycle."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    access,
    agent,
    auth,
    cache as cache_api,
    commands,
    config as config_api,
    integrations,
    jobs as jobs_api,
    knowledge,
    llm,
    logs,
    mcp as mcp_api,
    memory,
    policy as policy_api,
    skills as skills_api,
    tasks,
)
from app import __version__, net
from app.bootstrap import run_bootstrap
from app.config import get_settings
from app.db import SessionLocal
from app.runtime import Runtime

log = logging.getLogger(__name__)


class _AttachTraceback(logging.Filter):
    """In debug mode, a warning or error logged while handling an exception
    carries that exception's traceback, so the log shows where it came from."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING and not record.exc_info:
            info = sys.exc_info()
            if info[0] is not None:
                record.exc_info = info
        return True


def configure_logging(level: str, *, debug: bool = False) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # The DDP reader logs every frame at DEBUG; that is too much even for us.
    logging.getLogger("websockets.client").setLevel(logging.WARNING)
    if debug:
        # The bot's own messages in full; libraries stay at INFO, which for
        # httpx is one line per request with its status.
        logging.getLogger().setLevel(min(logging.getLogger().level, logging.INFO))
        logging.getLogger("app").setLevel(logging.DEBUG)
        for handler in logging.getLogger().handlers:
            if not any(isinstance(f, _AttachTraceback) for f in handler.filters):
                handler.addFilter(_AttachTraceback())


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, debug=settings.debug)
    log.info("Rocket.Chat bot %s starting%s", __version__, " in DEBUG mode" if settings.debug else "")
    net.log_summary()

    await run_bootstrap(SessionLocal, settings)
    runtime = Runtime.build(SessionLocal)
    app.state.runtime = runtime
    await runtime.start()
    log.info("Portal API ready")
    try:
        yield
    finally:
        await runtime.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Rocket.Chat Bot Portal",
        description="Configure the chatbot, its API integrations and reminders.",
        version=__version__,
        lifespan=lifespan,
        # Unhandled errors answer with their traceback instead of a bare 500.
        debug=settings.debug,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(config_api.router)
    app.include_router(commands.router)
    app.include_router(integrations.router)
    app.include_router(llm.router)
    app.include_router(agent.router)
    app.include_router(memory.router)
    app.include_router(knowledge.router)
    app.include_router(cache_api.router)
    app.include_router(jobs_api.router)
    app.include_router(policy_api.router)
    app.include_router(mcp_api.router)
    app.include_router(skills_api.router)
    app.include_router(tasks.router)
    app.include_router(logs.router)
    app.include_router(access.router)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, object]:
        """Liveness for Docker; never requires auth."""
        runtime: Runtime | None = getattr(app.state, "runtime", None)
        return {
            "status": "ok",
            "version": __version__,
            "debug": settings.debug,
            "bot_connected": bool(runtime and runtime.bot.status.connected),
            "scheduler_running": bool(runtime and runtime.scheduler.running),
        }

    return app


app = create_app()
