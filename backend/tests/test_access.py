"""Access control: who the bot will answer."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.bot.access import AccessPolicy, normalize_username
from app.models import BotAccessEntry, BotConfig, MessageLog
from app.rocketchat.ddp import IncomingMessage


def incoming(text: str, *, sender: str = "bo", room_type: str = "d", message_id: str = "M1"):
    return IncomingMessage(
        message_id=message_id,
        room_id="R1",
        text=text,
        sender_id=f"id-{sender}",
        sender_username=sender,
        room_type=room_type,
    )


async def set_access(runtime, mode: str, usernames=()):
    async with runtime.session_factory() as session:
        config = await session.get(BotConfig, 1)
        config.access_mode = mode
        config.config_version += 1
        session.add_all(BotAccessEntry(username=name) for name in usernames)
        await session.commit()
    runtime.config_store.invalidate()


async def message_logs(runtime) -> list[MessageLog]:
    async with runtime.session_factory() as session:
        return list((await session.scalars(select(MessageLog))).all())


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("mode", "user", "allowed"),
    [
        ("everyone", "anyone", True),
        ("everyone", None, True),
        ("allowlist", "an", True),
        ("allowlist", "AN", True),
        ("allowlist", "bo", False),
        ("allowlist", None, False),
        ("blocklist", "an", False),
        ("blocklist", "bo", True),
        ("blocklist", None, True),
    ],
)
def test_policy(mode, user, allowed):
    assert AccessPolicy(mode=mode, usernames=frozenset({"an"})).allows(user) is allowed


@pytest.mark.parametrize(("raw", "expected"), [("@An", "an"), (" bo.le ", "bo.le"), ("việt", "việt")])
def test_usernames_are_normalised(raw, expected):
    assert normalize_username(raw) == expected


@pytest.mark.parametrize("raw", ["", "@", "two words", "semi;colon"])
def test_invalid_usernames_are_rejected(raw):
    with pytest.raises(ValueError):
        normalize_username(raw)


# --------------------------------------------------------------------------- #
# Through the bot
# --------------------------------------------------------------------------- #


async def test_everyone_is_answered_by_default(app_env, gateway):
    await app_env.bot._handle(incoming("help", sender="stranger"))
    assert "What I can do" in gateway.last_text


async def test_an_allowlisted_user_is_answered(app_env, gateway):
    await set_access(app_env, "allowlist", ["an"])
    await app_env.bot._handle(incoming("help", sender="An"))
    assert "What I can do" in gateway.last_text
    assert (await message_logs(app_env))[0].status == "ok"


async def test_an_unlisted_user_gets_no_reply(app_env, gateway):
    await set_access(app_env, "allowlist", ["an"])
    await app_env.bot._handle(incoming("help", sender="bo"))

    assert gateway.sent == []
    log = (await message_logs(app_env))[0]
    assert log.status == "denied" and log.matched_command is None and log.text_out in (None, "")


async def test_a_blocked_user_gets_no_reply_even_for_unknown_text(app_env, gateway):
    await set_access(app_env, "blocklist", ["bo"])
    await app_env.bot._handle(incoming("tell me a joke", sender="bo"))

    assert gateway.sent == [] and gateway.updates == []
    assert (await message_logs(app_env))[0].status == "denied"


async def test_a_blocked_user_cannot_create_reminders(app_env, gateway):
    from app.models import ScheduledTask

    await set_access(app_env, "blocklist", ["bo"])
    await app_env.bot._handle(incoming("/remind in 5m stretch", sender="bo"))

    async with app_env.session_factory() as session:
        assert (await session.scalars(select(ScheduledTask))).all() == []


async def test_refused_users_chatting_in_a_channel_are_not_answered(app_env, gateway):
    await set_access(app_env, "allowlist", ["an"])
    await app_env.bot._handle(incoming("just chatting", sender="bo", room_type="c"))
    assert gateway.sent == []


# --------------------------------------------------------------------------- #
# Portal API
# --------------------------------------------------------------------------- #


async def test_adding_listing_and_removing_users(admin_client):
    added = await admin_client.post(
        "/api/access", json={"usernames": "@An, bo\ncuong bo", "note": "pilot team"}
    )
    assert added.status_code == 201
    assert sorted(e["username"] for e in added.json()) == ["an", "bo", "cuong"]

    again = await admin_client.post("/api/access", json={"usernames": "an"})
    assert again.json() == []

    listed = (await admin_client.get("/api/access")).json()
    assert [e["username"] for e in listed] == ["an", "bo", "cuong"]
    assert listed[0]["note"] == "pilot team"

    renamed = await admin_client.put(f"/api/access/{listed[0]['id']}", json={"note": "lead"})
    assert renamed.json()["note"] == "lead"

    assert (await admin_client.delete(f"/api/access/{listed[0]['id']}")).status_code == 204
    assert len((await admin_client.get("/api/access")).json()) == 2


async def test_bad_usernames_are_rejected_whole(admin_client):
    response = await admin_client.post("/api/access", json={"usernames": "an, not/valid"})
    assert response.status_code == 422 and "not/valid" in response.json()["detail"]
    assert (await admin_client.get("/api/access")).json() == []


async def test_access_mode_is_set_through_the_config(admin_client):
    body = (await admin_client.get("/api/config")).json()
    assert body["access_mode"] == "everyone"

    saved = await admin_client.put("/api/config", json={"access_mode": "allowlist"})
    assert saved.json()["access_mode"] == "allowlist"
    assert "access_denied_reply" not in saved.json()
    assert (await admin_client.put("/api/config", json={"access_mode": "vip"})).status_code == 422


async def test_check_reports_whether_a_user_would_be_answered(admin_client):
    await admin_client.put("/api/config", json={"access_mode": "allowlist"})
    await admin_client.post("/api/access", json={"usernames": "an"})

    yes = (await admin_client.get("/api/access/check", params={"username": "@AN"})).json()
    no = (await admin_client.get("/api/access/check", params={"username": "bo"})).json()
    assert (yes["allowed"], yes["reason"]) == (True, "on the allowlist")
    assert (no["allowed"], no["reason"]) == (False, "not on the allowlist")


async def test_list_changes_apply_without_a_restart(admin_client, app_env, gateway):
    await admin_client.put("/api/config", json={"access_mode": "blocklist"})
    await admin_client.post("/api/access", json={"usernames": "bo"})
    await app_env.bot._handle(incoming("help", sender="bo", message_id="M1"))
    assert (await message_logs(app_env))[-1].status == "denied"

    entry = (await admin_client.get("/api/access")).json()[0]
    await admin_client.delete(f"/api/access/{entry['id']}")
    await app_env.bot._handle(incoming("help", sender="bo", message_id="M2"))
    assert (await message_logs(app_env))[-1].status == "ok"


async def test_the_command_tester_reports_a_refused_user(admin_client):
    await admin_client.put("/api/config", json={"access_mode": "allowlist"})
    result = (
        await admin_client.post(
            "/api/commands/test", json={"text": "help", "username": "bo", "dry_run": False}
        )
    ).json()
    assert result["error"] == "@bo is not allowed to use the bot (no reply is sent)"
    assert not result["reply"]


async def test_viewers_can_read_the_list_but_not_change_it(admin_client):
    await admin_client.post(
        "/api/auth/users",
        json={"email": "viewer@example.com", "password": "viewer-password", "role": "viewer"},
    )
    login = await admin_client.post(
        "/api/auth/login", json={"email": "viewer@example.com", "password": "viewer-password"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert (await admin_client.get("/api/access", headers=headers)).status_code == 200
    forbidden = await admin_client.post("/api/access", json={"usernames": "an"}, headers=headers)
    assert forbidden.status_code == 403
