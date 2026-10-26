"""Sandboxed Jinja rendering for portal-authored templates.

Portal admins write templates for replies, URLs, headers and request bodies.
These are evaluated server-side, so they run in Jinja's SandboxedEnvironment
with autoescape off (we emit chat text and JSON, not HTML) and no access to
filesystem loaders.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from jinja2 import ChainableUndefined, StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment, SecurityError


class TemplateRenderError(ValueError):
    """A template failed to compile or render."""


def _to_json(value: Any, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, default=str)


def _from_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


class _Sandbox(SandboxedEnvironment):
    """Raise on sandbox violations instead of rendering them as empty.

    Jinja's default is to return an Undefined carrying a SecurityError, which
    a lenient Undefined class then stringifies to "". We would rather see the
    violation in the logs than silently swallow it.
    """

    def unsafe_undefined(self, obj, attribute):  # noqa: ANN001, ANN201
        raise SecurityError(
            f"access to attribute {attribute!r} of "
            f"{type(obj).__name__!r} object is unsafe"
        )


def _env(strict: bool) -> SandboxedEnvironment:
    env = _Sandbox(
        autoescape=False,
        undefined=StrictUndefined if strict else ChainableUndefined,
        keep_trailing_newline=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["tojson"] = _to_json
    env.filters["fromjson"] = _from_json
    env.globals["now"] = lambda: datetime.now(timezone.utc)
    return env


# Default is lenient: a missing key renders empty rather than breaking a reply
# mid-conversation. The portal's "Test" button renders with strict=True so
# authors see their mistakes.
_LENIENT = _env(strict=False)
_STRICT = _env(strict=True)


def render(template: str, context: dict[str, Any], *, strict: bool = False) -> str:
    """Render a template string, raising TemplateRenderError on any failure."""
    if not template:
        return ""
    env = _STRICT if strict else _LENIENT
    try:
        return env.from_string(template).render(**context)
    except TemplateError as exc:
        raise TemplateRenderError(f"{type(exc).__name__}: {exc}") from exc
    except Exception as exc:  # sandbox violations, recursion, bad filters
        raise TemplateRenderError(str(exc)) from exc


def render_json_object(
    template: str, context: dict[str, Any], *, strict: bool = False
) -> dict[str, Any]:
    """Render a template expected to produce a JSON object (headers, query)."""
    rendered = render(template, context, strict=strict).strip()
    if not rendered:
        return {}
    try:
        value = json.loads(rendered)
    except ValueError as exc:
        raise TemplateRenderError(f"template did not produce valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise TemplateRenderError("template must produce a JSON object")
    return {str(k): v for k, v in value.items()}


def validate(template: str) -> str | None:
    """Compile-check a template. Returns an error message, or None if valid."""
    if not template:
        return None
    try:
        _LENIENT.from_string(template)
    except TemplateError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None
