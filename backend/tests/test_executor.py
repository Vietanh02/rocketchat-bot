"""Integration execution: param binding, SSRF guard, and request/response shaping."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import get_settings
from app.integrations.executor import (
    IntegrationError,
    IntegrationSpec,
    ParamSpec,
    assert_url_allowed,
    bind_params,
    execute_integration,
)


def spec(**overrides) -> IntegrationSpec:
    base = dict(
        id=1,
        name="demo",
        description="A demo API",
        base_url="https://api.example.com",
        method="GET",
    )
    base.update(overrides)
    return IntegrationSpec(**base)


def stub(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# Parameter binding
# --------------------------------------------------------------------------- #


def test_positional_binding():
    params = [ParamSpec(name="a"), ParamSpec(name="b")]
    assert bind_params(params, ["1", "2"]) == {"a": "1", "b": "2"}


def test_greedy_param_swallows_the_rest():
    params = [ParamSpec(name="city", required=True), ParamSpec(name="note", greedy=True)]
    assert bind_params(params, ["Hanoi", "a", "b"]) == {"city": "Hanoi", "note": "a b"}


def test_named_values_win_over_positional():
    params = [ParamSpec(name="env"), ParamSpec(name="svc")]
    bound = bind_params(params, ["ignored"], named={"env": "prod", "svc": "api"})
    assert bound == {"env": "prod", "svc": "api"}


def test_defaults_fill_missing_optionals():
    params = [ParamSpec(name="units", default_value="metric")]
    assert bind_params(params, []) == {"units": "metric"}


def test_missing_required_param_is_reported():
    with pytest.raises(IntegrationError) as excinfo:
        bind_params([ParamSpec(name="city", required=True)], [])
    assert "city" in str(excinfo.value)


def test_usage_string_marks_required_and_optional():
    s = spec(params=[ParamSpec(name="city", required=True), ParamSpec(name="units")])
    assert s.usage() == "<city> [units]"


def test_tool_schema_is_claude_shaped():
    s = spec(params=[ParamSpec(name="city", required=True, description="City")])
    schema = s.to_tool_schema()
    assert schema["name"] == "demo"
    assert schema["input_schema"]["required"] == ["city"]
    assert schema["input_schema"]["properties"]["city"]["description"] == "City"


# --------------------------------------------------------------------------- #
# SSRF guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "https:///nohost"])
async def test_non_http_and_hostless_urls_are_refused(url):
    with pytest.raises(IntegrationError):
        await assert_url_allowed(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data/"])
async def test_private_targets_are_blocked_when_not_allowed(url, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "allow_private_integration_hosts", False)
    with pytest.raises(IntegrationError) as excinfo:
        await assert_url_allowed(url)
    assert "private" in str(excinfo.value)


async def test_private_targets_are_allowed_when_opted_in(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "allow_private_integration_hosts", True)
    await assert_url_allowed("http://127.0.0.1:3000/api")  # must not raise


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


async def test_get_request_renders_path_query_and_reply():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"temp": 26.7, "unit": "C"})

    result = await execute_integration(
        spec(
            path_template="/v1/weather/{{ params.city }}",
            query_template='{"units": "metric"}',
            headers_template='{"X-Trace": "{{ user.username }}"}',
            reply_template="{{ params.city }} is {{ response.temp }}{{ response.unit }}",
        ),
        params={"city": "Hanoi"},
        base_context={"user": {"username": "bo"}},
        client=stub(handler),
    )

    assert result.ok and result.status_code == 200
    assert result.text == "Hanoi is 26.7C"
    assert seen["url"] == "https://api.example.com/v1/weather/Hanoi?units=metric"
    assert seen["headers"]["x-trace"] == "bo"


async def test_post_sends_a_json_body():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 9})

    result = await execute_integration(
        spec(
            method="POST",
            path_template="/tickets",
            body_template='{"title": "{{ params.title }}", "by": "{{ user.username }}"}',
            reply_template="Created #{{ response.id }}",
        ),
        params={"title": "Printer broken"},
        base_context={"user": {"username": "bo"}},
        client=stub(handler),
    )

    assert seen["body"] == {"title": "Printer broken", "by": "bo"}
    assert result.text == "Created #9"


@pytest.mark.parametrize(
    ("auth_type", "expected_header", "expected_value"),
    [
        ("bearer", "authorization", "Bearer s3cret"),
        ("api_key", "x-api-key", "s3cret"),
    ],
)
async def test_auth_is_applied_to_headers(auth_type, expected_header, expected_value):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={})

    await execute_integration(
        spec(auth_type=auth_type, auth_secret="s3cret", reply_template="ok"),
        params={},
        client=stub(handler),
    )
    assert seen[expected_header] == expected_value


async def test_http_error_is_reported_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream is down")

    result = await execute_integration(
        spec(reply_template="never rendered"), params={}, client=stub(handler)
    )
    assert result.ok is False
    assert result.status_code == 503
    assert "503" in result.text and "upstream is down" in result.text


async def test_missing_reply_template_shows_the_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"a": 1})

    result = await execute_integration(spec(), params={}, client=stub(handler))
    assert '"a": 1' in result.text


async def test_transport_failure_becomes_an_integration_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(IntegrationError) as excinfo:
        await execute_integration(spec(), params={}, client=stub(handler))
    assert "failed" in str(excinfo.value)


async def test_broken_template_becomes_an_integration_error():
    with pytest.raises(IntegrationError) as excinfo:
        await execute_integration(
            spec(query_template="not json"), params={}, client=stub(lambda r: httpx.Response(200))
        )
    assert "template error" in str(excinfo.value)


async def test_unsupported_method_is_refused():
    with pytest.raises(IntegrationError):
        await execute_integration(spec(method="TRACE"), params={}, client=stub(lambda r: httpx.Response(200)))
