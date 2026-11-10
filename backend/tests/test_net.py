"""Proxy, certificate checks and error text (app.net), and debug mode."""

from __future__ import annotations

import logging
import socket
import ssl

import httpx
import pytest

from app import net
from app.config import get_settings

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


@pytest.fixture
def proxy_env(monkeypatch):
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://user:secret@proxy.corp:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy.corp:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,embed,rocketchat,.lan.corp")
    return monkeypatch


def test_proxy_for_follows_no_proxy(proxy_env):
    assert net.proxy_for("https://gateway.example.com/v1") == "http://user:secret@proxy.corp:3128"
    assert net.proxy_for("wss://chat.example.com/websocket")
    assert net.proxy_for("http://embed:80") is None
    assert net.proxy_for("ws://rocketchat:3000/websocket") is None
    assert net.proxy_for("https://wiki.lan.corp/x") is None
    assert net.route("http://embed:80") == "direct"
    # The password never reaches a log line.
    assert net.route("https://api.example.com") == "via proxy http://user:***@proxy.corp:3128"


def test_no_proxy_set(monkeypatch):
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    assert net.proxy_for("https://api.example.com") is None
    assert "no proxy" in net.summary()[0]


def test_proxy_env_is_handed_to_children(proxy_env, monkeypatch):
    monkeypatch.setattr(get_settings(), "tls_verify", False)
    env = net.proxy_env()
    assert env["HTTPS_PROXY"].startswith("http://")
    assert env["NO_PROXY"].startswith("localhost")
    assert env["NODE_TLS_REJECT_UNAUTHORIZED"] == "0"

    from app.bot.scripts import build_env

    script_env = build_env({})
    assert script_env["HTTPS_PROXY"] == env["HTTPS_PROXY"]
    assert script_env["BOT_TLS_VERIFY"] == "0"


def test_tls_verify_switch(monkeypatch):
    assert net.httpx_verify() is True
    assert net.ssl_context().verify_mode == ssl.CERT_REQUIRED
    assert net.websocket_ssl("ws://rocketchat:3000/websocket") is None

    monkeypatch.setattr(get_settings(), "tls_verify", False)
    assert net.httpx_verify() is False
    context = net.websocket_ssl("wss://chat.example.com/websocket")
    assert context is not None and context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False
    assert "OFF" in net.summary()[-1]


def test_describe_error_walks_the_causes():
    try:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed: self-signed")
        except ssl.SSLError as inner:
            raise httpx.ConnectError("[SSL] handshake failed") from inner
    except httpx.ConnectError as exc:
        wrapped = RuntimeError("Connection error.")
        wrapped.__cause__ = exc
        text = net.describe_error(wrapped)
    assert text.startswith("RuntimeError: Connection error.")
    assert "ConnectError: [SSL] handshake failed" in text
    assert "self-signed" in text

    group = ExceptionGroup("unhandled errors", [ConnectionRefusedError("refused")])
    assert net.describe_error(group) == "ConnectionRefusedError: refused"


async def test_private_check_trusts_the_proxy_for_unresolvable_names(proxy_env, monkeypatch):
    from app.integrations import executor

    monkeypatch.setattr(get_settings(), "allow_private_integration_hosts", False)

    async def no_dns(*args, **kwargs):
        raise socket.gaierror(-3, "Temporary failure in name resolution")

    import asyncio

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", no_dns)
    # Through the proxy: the proxy resolves the name.
    await executor.assert_url_allowed("https://gateway.example.com/v1")
    # Not through it: an unknown name is still an error.
    with pytest.raises(executor.IntegrationError, match="cannot resolve"):
        await executor.assert_url_allowed("https://wiki.lan.corp/x")


def test_llm_error_shows_the_cause_only_in_debug(monkeypatch):
    from app.bot.llm import LLMError

    plain = LLMError("could not reach the language model", "ConnectError: refused")
    assert str(plain) == "could not reach the language model"
    assert plain.detail == "ConnectError: refused"

    monkeypatch.setattr(get_settings(), "debug", True)
    debug = LLMError("could not reach the language model", "ConnectError: refused")
    assert str(debug) == "could not reach the language model (ConnectError: refused)"


def test_debug_logging_attaches_tracebacks():
    from app.main import _AttachTraceback

    attach = _AttachTraceback()
    record = logging.LogRecord("app.x", logging.WARNING, __file__, 1, "failed", None, None)
    try:
        raise ValueError("boom")
    except ValueError:
        attach.filter(record)
    assert record.exc_info and record.exc_info[0] is ValueError

    quiet = logging.LogRecord("app.x", logging.INFO, __file__, 1, "fine", None, None)
    try:
        raise ValueError("boom")
    except ValueError:
        attach.filter(quiet)
    assert not quiet.exc_info
