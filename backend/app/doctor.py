"""Check every connection the bot makes, and say which one fails and why.

    docker compose exec backend python -m app.doctor
    docker compose exec backend python -m app.doctor --chat   # also ask the model a question

Each line says whether the target is reached directly or through the proxy,
and for a failure, the whole chain of causes (a refused proxy, a failed
certificate check, a name that does not resolve). Nothing is changed; only
--chat spends tokens. The exit status is 1 when a check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable

import httpx
import websockets
from sqlalchemy import select, text

from app import __version__, net
from app.config import get_settings

CHECK_TIMEOUT_SECONDS = 20.0


class Doctor:
    def __init__(self) -> None:
        self.failed = 0
        self.http = httpx.AsyncClient(timeout=15.0, verify=net.httpx_verify())

    def line(self, status: str, name: str, target: str, detail: str) -> None:
        where = f"  {target}  [{net.route(target)}]" if target.startswith(("http", "ws")) else ""
        print(f"[{status:^4}] {name}{where}", flush=True)
        if detail:
            print(f"       {detail}", flush=True)

    async def check(
        self, name: str, target: str, probe: Callable[[], Awaitable[str]]
    ) -> None:
        started = time.monotonic()
        try:
            async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
                detail = await probe()
        except TimeoutError:
            self.failed += 1
            self.line("FAIL", name, target, f"no answer within {CHECK_TIMEOUT_SECONDS:g}s")
            return
        except Exception as exc:  # noqa: BLE001 - reporting is the point
            self.failed += 1
            self.line("FAIL", name, target, net.describe_error(exc))
            return
        took = int((time.monotonic() - started) * 1000)
        self.line("OK", name, target, f"{detail} ({took} ms)" if detail else f"{took} ms")

    def skip(self, name: str, why: str) -> None:
        self.line("SKIP", name, "", why)


async def run(ask_model: bool) -> int:
    settings = get_settings()
    print(f"Rocket.Chat bot {__version__} - connection check")
    print(f"debug={'on' if settings.debug else 'off'}")
    for fact in net.summary():
        print(fact)
    print()

    doctor = Doctor()
    from app.db import SessionLocal

    async def database() -> str:
        async with SessionLocal() as session:
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        return f"revision {revision}"

    await doctor.check("Database", settings.database_url.split("///")[-1], database)
    if doctor.failed:
        return 1

    from app.bot.config_store import ConfigStore

    snapshot = await ConfigStore(SessionLocal).reload()
    bot = snapshot.settings

    # --- Rocket.Chat ---------------------------------------------------- #
    if bot.rc_rest_url:
        rest_url = bot.rc_rest_url.rstrip("/")

        async def rc_info() -> str:
            response = await doctor.http.get(f"{rest_url}/api/info")
            response.raise_for_status()
            version = response.json().get("version")
            return f"Rocket.Chat {version}" if version else "reachable"

        await doctor.check("Rocket.Chat REST", rest_url, rc_info)

        if bot.bot_user_id and bot.auth_token:

            async def rc_me() -> str:
                response = await doctor.http.get(
                    f"{rest_url}/api/v1/me",
                    headers={"X-User-Id": bot.bot_user_id or "", "X-Auth-Token": bot.auth_token or ""},
                )
                if response.status_code == 401:
                    raise RuntimeError("the bot's user id or token is rejected (HTTP 401)")
                response.raise_for_status()
                return f"logged in as @{response.json().get('username', '?')}"

            await doctor.check("Rocket.Chat bot login", rest_url, rc_me)
    else:
        doctor.skip("Rocket.Chat REST", "no URL set (portal: Bot settings)")

    if bot.rc_ws_url:

        async def rc_ws() -> str:
            async with websockets.connect(
                bot.rc_ws_url, ssl=net.websocket_ssl(bot.rc_ws_url), open_timeout=15
            ) as ws:
                await ws.send(json.dumps({"msg": "connect", "version": "1", "support": ["1"]}))
                while True:
                    frame = json.loads(await ws.recv())
                    if frame.get("msg") == "connected":
                        return "realtime session opened"
                    if frame.get("msg") == "failed":
                        raise RuntimeError(f"server refused the session: {frame}")

        await doctor.check("Rocket.Chat realtime", bot.rc_ws_url, rc_ws)
    else:
        doctor.skip("Rocket.Chat realtime", "no URL set (portal: Bot settings)")

    # --- Language model ------------------------------------------------- #
    llm = snapshot.llm
    if llm.configured:
        base = llm.base_url.rstrip("/")

        async def models() -> str:
            response = await doctor.http.get(
                f"{base}/models", headers={"Authorization": f"Bearer {llm.api_key}"}
            )
            if response.status_code in (401, 403):
                raise RuntimeError(f"the API key is rejected (HTTP {response.status_code})")
            if response.status_code == 404:
                return "reachable (it has no /models list)"
            response.raise_for_status()
            names = [m.get("id") for m in response.json().get("data", []) if isinstance(m, dict)]
            listed = "listed" if llm.model in names else "NOT in its list"
            return f"{len(names)} models; '{llm.model}' is {listed}"

        await doctor.check("LLM gateway", base, models)

        if ask_model:
            from app.bot.llm import LLMChat

            async def chat() -> str:
                client = LLMChat.from_settings(llm)
                assert client is not None
                try:
                    answer = await client.complete(
                        client.build_messages("Reply with the single word OK.", [], bot_name="the bot")
                    )
                finally:
                    await client.aclose()
                return f"'{llm.model}' answered: {answer.strip()[:80]!r}"

            await doctor.check("LLM answer", base, chat)
    else:
        doctor.skip("LLM gateway", "not set up (portal: Language model)")

    # --- Embedding, reranker, Qdrant ------------------------------------ #
    embedding = snapshot.embedding
    from app.knowledge.embeddings import Embedder, Reranker

    if embedding.configured:

        async def embed() -> str:
            # As a document: the full timeout, not the short one of a question.
            vectors = await Embedder(embedding, doctor.http).embed_documents(["connection check"])
            return f"{len(vectors[0])} dimensions"

        await doctor.check("Embedding", embedding.base_url, embed)
    else:
        doctor.skip("Embedding", "not set up (portal: Memory)")

    if embedding.rerank_configured:

        async def rerank() -> str:
            scores = await Reranker(embedding, doctor.http).rerank("check", ["a check", "other"])
            return f"{len(scores)} scores"

        await doctor.check("Reranker", embedding.rerank_base_url, rerank)

    if settings.qdrant_url:
        from app.knowledge.vectorstore import QdrantStore

        async def qdrant() -> str:
            store = QdrantStore(
                settings.qdrant_url, http=doctor.http, api_key=settings.qdrant_api_key
            )
            return f"Qdrant {await store.ping()}"

        await doctor.check("Qdrant", settings.qdrant_url, qdrant)
    else:
        doctor.skip("Qdrant", "APP_QDRANT_URL is not set")

    # --- MCP servers and integrations ----------------------------------- #
    from app.integrations.mcp import McpServerSpec, list_tools
    from app.models import McpServer

    async with SessionLocal() as session:
        servers = list(
            (await session.scalars(select(McpServer).where(McpServer.enabled.is_(True)))).all()
        )
    for server in servers:
        spec = McpServerSpec.from_model(server)

        async def mcp(spec: McpServerSpec = spec) -> str:
            return f"{len(await list_tools(spec))} tools"

        await doctor.check(
            f"MCP {spec.name}", spec.url if spec.url else f"(stdio) {spec.command}", mcp
        )

    from app.integrations.executor import assert_url_allowed

    for spec in snapshot.integrations.values():
        # Not called: a request could change something. Only the address rules.
        async def allowed(url: str = spec.base_url) -> str:
            await assert_url_allowed(url)
            return "address allowed (not called)"

        await doctor.check(f"Integration {spec.name}", spec.base_url, allowed)

    await doctor.http.aclose()
    print()
    if doctor.failed:
        print(f"{doctor.failed} check(s) failed.")
        if not settings.debug:
            print("For full tracebacks in the backend log, start it with APP_DEBUG=true.")
        return 1
    print("All checks passed.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.doctor", description=__doc__.split("\n")[0])
    parser.add_argument("--chat", action="store_true", help="also ask the model a short question")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every request")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-8s %(name)s: %(message)s",
    )
    sys.exit(asyncio.run(run(args.chat)))


if __name__ == "__main__":
    main()
