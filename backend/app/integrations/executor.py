"""Calls portal-configured HTTP APIs and renders their responses as chat text."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.config import get_settings
from app.integrations.templating import (
    TemplateRenderError,
    render,
    render_json_object,
)
from app.models import AuthType, Integration
from app.security import decrypt_secret

log = logging.getLogger(__name__)

MAX_RESPONSE_CHARS = 4000
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}


class IntegrationError(RuntimeError):
    """The integration could not be executed or returned a failure."""


# --------------------------------------------------------------------------- #
# Specs (detached from the ORM so they are safe to cache and pass around)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ParamSpec:
    name: str
    description: str = ""
    required: bool = False
    default_value: str = ""
    greedy: bool = False


@dataclass(slots=True)
class IntegrationSpec:
    id: int
    name: str
    description: str
    base_url: str
    method: str
    path_template: str = ""
    query_template: str = ""
    headers_template: str = ""
    body_template: str = ""
    reply_template: str = ""
    auth_type: str = AuthType.none.value
    auth_username: str | None = None
    auth_header_name: str | None = None
    auth_secret: str | None = None  # decrypted
    timeout_seconds: float = 10.0
    params: list[ParamSpec] = field(default_factory=list)

    @classmethod
    def from_model(cls, model: Integration) -> IntegrationSpec:
        return cls(
            id=model.id,
            name=model.name,
            description=model.description or "",
            base_url=model.base_url,
            method=(model.method or "GET").upper(),
            path_template=model.path_template or "",
            query_template=model.query_template or "",
            headers_template=model.headers_template or "",
            body_template=model.body_template or "",
            reply_template=model.reply_template or "",
            auth_type=model.auth_type,
            auth_username=model.auth_username,
            auth_header_name=model.auth_header_name,
            auth_secret=decrypt_secret(model.auth_secret_enc),
            timeout_seconds=model.timeout_seconds or 10.0,
            params=[
                ParamSpec(
                    name=p.name,
                    description=p.description or "",
                    required=p.required,
                    default_value=p.default_value or "",
                    greedy=p.greedy,
                )
                for p in sorted(model.params, key=lambda p: p.position)
            ],
        )

    def usage(self) -> str:
        parts = [
            f"<{p.name}>" if p.required else f"[{p.name}]" for p in self.params
        ]
        return " ".join(parts)

    def to_tool_schema(self) -> dict[str, Any]:
        """Claude tool definition for this integration.

        Unused while the responder is rule-based; it is what an LLM responder
        will hand to the Messages API so no schema change is needed later.
        """
        return {
            "name": self.name,
            "description": self.description or f"Call the {self.name} API",
            "input_schema": {
                "type": "object",
                "properties": {
                    p.name: {
                        "type": "string",
                        "description": p.description or p.name,
                    }
                    for p in self.params
                },
                "required": [p.name for p in self.params if p.required],
            },
        }


@dataclass(slots=True)
class IntegrationResult:
    ok: bool
    text: str
    status_code: int | None = None
    data: Any = None
    url: str | None = None
    error: str | None = None
    duration_ms: int | None = None


# --------------------------------------------------------------------------- #
# Parameter binding
# --------------------------------------------------------------------------- #


def bind_params(
    specs: list[ParamSpec],
    argv: list[str],
    *,
    named: dict[str, str] | None = None,
) -> dict[str, str]:
    """Map command arguments onto the integration's declared parameters.

    Named values (regex capture groups, or portal-supplied args) win; the rest
    are filled positionally, with a trailing `greedy` param swallowing what is
    left over.
    """
    named = named or {}
    values: dict[str, str] = {}
    remaining = list(argv)

    for spec in specs:
        if spec.name in named and named[spec.name] != "":
            values[spec.name] = named[spec.name]
            continue
        if spec.greedy:
            values[spec.name] = " ".join(remaining).strip()
            remaining = []
        elif remaining:
            values[spec.name] = remaining.pop(0)
        else:
            values[spec.name] = spec.default_value

        if not values[spec.name]:
            values[spec.name] = spec.default_value

    missing = [s.name for s in specs if s.required and not values.get(s.name)]
    if missing:
        raise IntegrationError(
            "Missing required argument(s): " + ", ".join(missing)
        )
    return values


# --------------------------------------------------------------------------- #
# SSRF guard
# --------------------------------------------------------------------------- #


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def assert_url_allowed(url: str) -> None:
    """Reject non-HTTP schemes and, by default, private network targets.

    Integration URLs are admin-authored, but an admin should still not be able
    to turn the bot into a probe of the host's internal network by accident.
    Set APP_ALLOW_PRIVATE_INTEGRATION_HOSTS=true for local development.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise IntegrationError(f"unsupported URL scheme: {parsed.scheme or '(none)'}")
    host = parsed.hostname
    if not host:
        raise IntegrationError("URL has no host")

    if get_settings().allow_private_integration_hosts:
        return

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise IntegrationError(f"cannot resolve host {host!r}: {exc}") from exc

    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise IntegrationError(
                f"host {host!r} resolves to the private address {address}; "
                "set APP_ALLOW_PRIVATE_INTEGRATION_HOSTS=true to allow this"
            )


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def _auth_headers(spec: IntegrationSpec) -> tuple[dict[str, str], httpx.Auth | None]:
    headers: dict[str, str] = {}
    auth: httpx.Auth | None = None
    secret = spec.auth_secret or ""

    if spec.auth_type == AuthType.bearer.value and secret:
        headers["Authorization"] = f"Bearer {secret}"
    elif spec.auth_type == AuthType.api_key.value and secret:
        headers[spec.auth_header_name or "X-API-Key"] = secret
    elif spec.auth_type == AuthType.basic.value and (spec.auth_username or secret):
        auth = httpx.BasicAuth(spec.auth_username or "", secret)
    return headers, auth


def _render_reply(spec: IntegrationSpec, context: dict[str, Any], strict: bool) -> str:
    if spec.reply_template:
        return render(spec.reply_template, context, strict=strict).strip()
    # No template configured: show the payload so the author can write one.
    data = context.get("response")
    body = (
        json.dumps(data, ensure_ascii=False, indent=2, default=str)
        if data is not None
        else str(context.get("body", ""))
    )
    return f"```\n{body[:MAX_RESPONSE_CHARS]}\n```"


async def execute_integration(
    spec: IntegrationSpec,
    *,
    params: dict[str, str],
    base_context: dict[str, Any] | None = None,
    strict: bool = False,
    client: httpx.AsyncClient | None = None,
) -> IntegrationResult:
    """Render the request from the spec, call it, and render the reply."""
    method = spec.method.upper()
    if method not in ALLOWED_METHODS:
        raise IntegrationError(f"unsupported HTTP method: {spec.method}")

    context: dict[str, Any] = {**(base_context or {}), "params": params}

    try:
        path = render(spec.path_template, context, strict=strict).strip()
        query = render_json_object(spec.query_template, context, strict=strict)
        headers = render_json_object(spec.headers_template, context, strict=strict)
        body_text = render(spec.body_template, context, strict=strict).strip()
    except TemplateRenderError as exc:
        raise IntegrationError(f"template error: {exc}") from exc

    url = urljoin(spec.base_url.rstrip("/") + "/", path.lstrip("/")) if path else spec.base_url
    await assert_url_allowed(url)

    auth_headers, auth = _auth_headers(spec)
    headers = {**auth_headers, **{str(k): str(v) for k, v in headers.items()}}

    request_kwargs: dict[str, Any] = {
        "params": {k: str(v) for k, v in query.items()} or None,
        "headers": headers or None,
    }
    if body_text and method in {"POST", "PUT", "PATCH", "DELETE"}:
        try:
            request_kwargs["json"] = json.loads(body_text)
        except ValueError:
            request_kwargs["content"] = body_text.encode("utf-8")
            headers.setdefault("Content-Type", "text/plain; charset=utf-8")
            request_kwargs["headers"] = headers

    owns_client = client is None
    client = client or httpx.AsyncClient(
        timeout=spec.timeout_seconds or get_settings().integration_timeout_seconds,
        follow_redirects=False,
    )
    started = asyncio.get_running_loop().time()
    try:
        response = await client.request(method, url, **request_kwargs)
    except httpx.HTTPError as exc:
        raise IntegrationError(f"request to {url} failed: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()
    duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)

    raw = response.text[: MAX_RESPONSE_CHARS * 4]
    try:
        data = response.json()
    except ValueError:
        data = None

    context.update(
        {
            "response": data,
            "body": raw,
            "status": response.status_code,
            "headers_out": dict(response.headers),
        }
    )

    if response.status_code >= 400:
        detail = (raw or response.reason_phrase or "").strip()[:300]
        return IntegrationResult(
            ok=False,
            text=f":warning: `{spec.name}` returned HTTP {response.status_code}"
            + (f"\n```\n{detail}\n```" if detail else ""),
            status_code=response.status_code,
            data=data,
            url=url,
            error=f"HTTP {response.status_code}",
            duration_ms=duration_ms,
        )

    try:
        text = _render_reply(spec, context, strict)
    except TemplateRenderError as exc:
        raise IntegrationError(f"reply template error: {exc}") from exc

    return IntegrationResult(
        ok=True,
        text=text[:MAX_RESPONSE_CHARS],
        status_code=response.status_code,
        data=data,
        url=url,
        duration_ms=duration_ms,
    )
