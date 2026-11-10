"""How the bot reaches other hosts: proxy, certificate checks, error text.

The proxy comes from the standard variables HTTP_PROXY, HTTPS_PROXY,
ALL_PROXY and NO_PROXY (either case), which httpx, websockets and most
tools read on their own. This module only reports what they will do, hands
the same variables to child processes, and turns certificate checks off
everywhere when APP_TLS_VERIFY=false.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from app.config import get_settings

log = logging.getLogger(__name__)

PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def tls_verify() -> bool:
    return get_settings().tls_verify


def httpx_verify() -> bool:
    """The `verify=` argument for httpx and httpx2 clients."""
    return tls_verify()


def ssl_context() -> ssl.SSLContext:
    """A client context that follows APP_TLS_VERIFY."""
    context = ssl.create_default_context()
    if not tls_verify():
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def websocket_ssl(url: str) -> ssl.SSLContext | None:
    """The `ssl=` argument for websockets.connect: None for ws://."""
    return ssl_context() if url.lower().startswith("wss://") else None


def proxy_env() -> dict[str, str]:
    """The proxy variables of this process, to pass on to child processes."""
    env = {name: os.environ[name] for name in PROXY_VARIABLES if os.environ.get(name)}
    if not tls_verify():
        # Node-based tools (MCP servers started with npx) honour this one.
        env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    return env


def mask(url: str) -> str:
    """The URL without the password, for logs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit(parts._replace(netloc=f"{parts.username}:***@{host}"))


def proxy_for(url: str) -> str | None:
    """The proxy a request to `url` goes through, or None when it goes direct.

    Follows urllib's reading of the variables, which httpx and websockets
    share for the common cases (host names, domains, `*`).
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.hostname
    if not host:
        return None
    proxies = urllib.request.getproxies_environment()
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme, parts.scheme)
    proxy = proxies.get(scheme) or proxies.get("all")
    if not proxy:
        return None
    netloc = f"{host}:{parts.port}" if parts.port else host
    if urllib.request.proxy_bypass_environment(netloc, proxies) or (
        urllib.request.proxy_bypass_environment(host, proxies)
    ):
        return None
    return proxy


def route(url: str) -> str:
    """'via proxy …' or 'direct', for logs and the doctor."""
    proxy = proxy_for(url)
    return f"via proxy {mask(proxy)}" if proxy else "direct"


def describe_error(exc: BaseException) -> str:
    """The whole chain of causes, outermost first, as one line.

    SDKs wrap transport errors in vague ones ("Connection error."); the real
    reason (a refused proxy, a failed certificate check) is further down.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 6:
        seen.add(id(current))
        # Exception groups (anyio, MCP) hide the cause one level down.
        inner = getattr(current, "exceptions", None)
        if inner:
            current = inner[0]
            continue
        if isinstance(current, asyncio.CancelledError):
            # How a timeout was carried out, not why it happened.
            current = current.__cause__ or current.__context__
            continue
        text = str(current).strip()
        part = f"{type(current).__name__}: {text}" if text else type(current).__name__
        if not parts or part != parts[-1]:
            parts.append(part)
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def summary() -> list[str]:
    """How outgoing connections are made, one fact per line."""
    lines = []
    shown = [(name, os.environ[name]) for name in PROXY_VARIABLES if os.environ.get(name)]
    if shown:
        for name, value in shown:
            lines.append(f"{name}={value if name.lower() == 'no_proxy' else mask(value)}")
    else:
        lines.append("no proxy is set (HTTP_PROXY / HTTPS_PROXY)")
    lines.append(
        "TLS certificates are checked"
        if tls_verify()
        else "TLS certificate checks are OFF (APP_TLS_VERIFY=false)"
    )
    return lines


def log_summary() -> None:
    for line in summary():
        log.info("Network: %s", line)
    if not tls_verify():
        log.warning(
            "Certificates of HTTPS and WSS servers are not checked; "
            "anyone on the path can read and change the traffic."
        )
