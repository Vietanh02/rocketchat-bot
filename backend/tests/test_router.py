"""Router matching: trigger kinds, modes, and priority order."""

from __future__ import annotations

import pytest

from app.bot.router import CompiledCommand, Router, compile_pattern, split_args


def make(**overrides) -> CompiledCommand:
    base = dict(
        command_id=overrides.pop("id", 1),
        name="cmd",
        description="",
        trigger_type="keyword",
        pattern="p",
        match_mode="prefix",
        case_sensitive=False,
        priority=100,
        show_in_help=True,
        response_type="static",
        static_response="",
        builtin_name=None,
        integration_id=None,
    )
    base.update(overrides)
    command = CompiledCommand(**base)
    if command.trigger_type == "regex":
        command.regex = compile_pattern(command.pattern, command.case_sensitive)
    return command


@pytest.mark.parametrize(
    ("text", "expected", "args"),
    [
        ("weather Hanoi", "weather", "Hanoi"),
        ("weather", "weather", ""),
        ("/weather Hanoi", "weather", "Hanoi"),  # a typed slash is tolerated
        ("WEATHER x", "weather", "x"),  # case-insensitive by default
        ("weather\nline two", "weather", "line two"),  # any whitespace ends the word
        ("weatherman", None, ""),
    ],
)
def test_prefix_keyword_takes_arguments(text, expected, args):
    router = Router([make(name="weather", pattern="weather")])
    result = router.match(text)
    assert (result.command.name if result else None) == expected
    if result:
        assert result.args_text == args


@pytest.mark.parametrize(
    ("mode", "text", "matches", "args"),
    [
        ("exact", "help", True, ""),
        ("exact", "/help", True, ""),  # a leading slash is tolerated
        ("exact", "help me", False, ""),
        ("prefix", "tasks", True, ""),
        ("prefix", "tasks delete 3", True, "delete 3"),
        ("prefix", "tasksfoo", False, ""),
        ("contains", "well hello there", True, "well hello there"),
        ("contains", "nothing", False, ""),
    ],
)
def test_keyword_modes(mode, text, matches, args):
    pattern = {"exact": "help", "prefix": "tasks", "contains": "hello"}[mode]
    router = Router([make(trigger_type="keyword", pattern=pattern, match_mode=mode)])
    result = router.match(text)
    assert bool(result) is matches
    if result:
        assert result.args_text == args


def test_regex_named_groups_become_params():
    router = Router(
        [
            make(
                trigger_type="regex",
                pattern=r"deploy (?P<service>\w+) to (?P<env>\w+)",
            )
        ]
    )
    result = router.match("please deploy api to staging now")
    assert result is not None
    assert result.named == {"service": "api", "env": "staging"}


def test_case_sensitive_regex_does_not_match_other_case():
    router = Router([make(trigger_type="regex", pattern="PING", case_sensitive=True)])
    assert router.match("ping") is None
    assert router.match("PING") is not None


def test_invalid_regex_is_dropped_not_fatal():
    command = make(trigger_type="regex", pattern="([unclosed")
    assert command.regex is None
    assert Router([command]).match("anything") is None


def test_lower_priority_number_wins():
    router = Router(
        [
            make(id=1, name="specific", trigger_type="keyword", pattern="deploy",
                 match_mode="contains", priority=10),
            make(id=2, name="catch-all", trigger_type="keyword", pattern="deploy",
                 match_mode="contains", priority=5),
        ]
    )
    assert router.match("deploy now").command.name == "catch-all"


def test_empty_message_matches_nothing():
    router = Router([make(trigger_type="keyword", pattern="hi", match_mode="contains")])
    assert router.match("   ") is None


def test_help_entries_respect_show_in_help():
    router = Router(
        [
            make(id=1, name="shown", pattern="a", description="visible"),
            make(id=2, name="hidden", pattern="b", show_in_help=False),
        ]
    )
    assert router.help_entries() == [("a …", "visible")]


def test_split_args_handles_quotes_and_bad_quoting():
    assert split_args('one "two three" four') == ["one", "two three", "four"]
    assert split_args('unbalanced "quote') == ["unbalanced", '"quote']


def test_oversized_pattern_is_rejected():
    assert compile_pattern("a" * 600, False) is None


def test_a_pattern_saved_with_a_slash_still_matches():
    router = Router([make(name="disk", pattern="/disk")])
    assert router.match("disk /var").args == ["/var"]
