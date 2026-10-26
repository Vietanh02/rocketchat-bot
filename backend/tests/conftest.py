"""Test fixtures.

Environment is configured before any app module is imported, because
`app.db` builds its engine at import time from the settings.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="rcbot-tests-"))

os.environ.update(
    APP_SECRET_KEY="test-secret-key-for-the-suite-0123456789",
    APP_DATABASE_URL=f"sqlite+aiosqlite:///{_TMP / 'test.db'}",
    APP_BOOTSTRAP_ADMIN_EMAIL="admin@example.com",
    APP_BOOTSTRAP_ADMIN_PASSWORD="test-admin-password",
    APP_ALLOW_PRIVATE_INTEGRATION_HOSTS="true",
    APP_RC_REST_URL="http://rocketchat.invalid:3000",
    APP_RC_WS_URL="ws://rocketchat.invalid:3000/websocket",
    APP_RC_BOT_USER_ID="",
    APP_RC_BOT_AUTH_TOKEN="",
    APP_RC_BOT_USERNAME="",
    APP_RC_BOT_PASSWORD="",
    APP_LOG_LEVEL="WARNING",
)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "test-admin-password"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture
async def app_env():
    """A fresh database and runtime for each test."""
    from sqlalchemy import text

    from app.bootstrap import run_bootstrap
    from app.config import get_settings
    from app.db import Base, SessionLocal, engine
    from app.runtime import Runtime

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await run_bootstrap(SessionLocal, get_settings())

    runtime = Runtime.build(SessionLocal)
    await runtime.scheduler.start()
    try:
        yield runtime
    finally:
        await runtime.stop()
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA foreign_keys=OFF"))
            await connection.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def gateway(app_env):
    """Swap in an in-memory gateway so nothing talks to Rocket.Chat."""
    from app.rocketchat.gateway import FakeGateway

    fake = FakeGateway()
    app_env.bot._gateway = fake
    app_env.scheduler.set_gateway(fake)
    return fake


@pytest_asyncio.fixture
async def client(app_env):
    """HTTP client bound to the app, sharing the fixture's runtime."""
    import httpx

    from app.main import create_app

    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.runtime = app_env

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http_client:
        yield http_client


def _noop_lifespan(app):  # the app_env fixture owns the runtime lifecycle
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx(_app):
        yield

    return _ctx(app)


@pytest_asyncio.fixture
async def admin_client(client):
    """A client with an admin bearer token already attached."""
    response = await client.post(
        "/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client
