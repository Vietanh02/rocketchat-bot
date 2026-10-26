"""Template rendering, including the sandbox boundary."""

from __future__ import annotations

import pytest

from app.integrations.templating import (
    TemplateRenderError,
    render,
    render_json_object,
    validate,
)


def test_renders_nested_context():
    out = render("Hi {{ user.username }}, args={{ args }}", {"user": {"username": "bo"}, "args": "x"})
    assert out == "Hi bo, args=x"


def test_missing_values_render_empty_by_default():
    assert render("[{{ nope }}][{{ nope.deeper.deepest }}]", {}) == "[][]"


def test_missing_values_raise_in_strict_mode():
    with pytest.raises(TemplateRenderError):
        render("{{ nope }}", {}, strict=True)


@pytest.mark.parametrize(
    "template",
    [
        "{{ ''.__class__.__mro__ }}",
        "{{ self.__init__.__globals__ }}",
        "{{ cycler.__init__.__globals__.os.popen('id').read() }}",
        "{{ namespace.__init__.__globals__ }}",
    ],
)
def test_sandbox_blocks_attribute_escapes(template):
    """A portal admin must not be able to reach the host through a template."""
    with pytest.raises(TemplateRenderError) as excinfo:
        render(template, {})
    assert "SecurityError" in str(excinfo.value)


def test_json_object_rendering():
    assert render_json_object('{"A": "{{ k }}", "n": 1}', {"k": "v"}) == {"A": "v", "n": 1}
    assert render_json_object("", {}) == {}


def test_json_object_rejects_non_object():
    with pytest.raises(TemplateRenderError):
        render_json_object("[1, 2]", {})
    with pytest.raises(TemplateRenderError):
        render_json_object("not json", {})


def test_validate_reports_syntax_errors_without_raising():
    assert validate("{{ fine }}") is None
    assert "TemplateSyntaxError" in validate("{{ broken")


def test_tojson_filter_is_available():
    assert render("{{ d | tojson }}", {"d": {"a": 1}}) == '{"a": 1}'
