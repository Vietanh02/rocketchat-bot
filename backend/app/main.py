"""FastAPI application: the portal API plus the bot and scheduler lifecycle."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import access, auth, commands, config as config_api, integrations, logs, tasks
from app.bootstrap import run_bootstrap
from app.config import get_settings
from app.db import SessionLocal
from app.runtime import Runtime

log = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # The DDP reader logs every frame at DEBUG; that is too much even for us.
    logging.getLogger("websockets.client").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

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
        version="0.1.0",
        lifespan=lifespan,
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
    app.include_router(tasks.router)
    app.include_router(logs.router)
    app.include_router(access.router)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, object]:
        """Liveness for Docker; never requires auth."""
        runtime: Runtime | None = getattr(app.state, "runtime", None)
        return {
            "status": "ok",
            "bot_connected": bool(runtime and runtime.bot.status.connected),
            "scheduler_running": bool(runtime and runtime.scheduler.running),
        }

    return app


app = create_app()
