"""Portal API: auth, permissions, CRUD and validation."""

from __future__ import annotations

import pytest

from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

COMMAND = {
    "name": "ping",
    "pattern": "ping",
    "response_type": "static",
    "static_response": "pong",
}


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


async def test_health_needs_no_auth(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    "path", ["/api/commands", "/api/config", "/api/tasks", "/api/integrations", "/api/dashboard"]
)
async def test_endpoints_require_a_token(client, path):
    assert (await client.get(path)).status_code == 401


async def test_a_garbage_token_is_rejected(client):
    response = await client.get(
        "/api/commands", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 401


async def test_login_and_me(client):
    response = await client.post(
        "/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "admin" and body["expires_in"] > 0

    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.json()["email"] == ADMIN_EMAIL


async def test_wrong_password_is_401_without_hinting(client):
    response = await client.post(
        "/api/auth/login", json={"email": ADMIN_EMAIL, "password": "nope"}
    )
    assert response.status_code == 401
    unknown = await client.post(
        "/api/auth/login", json={"email": "ghost@example.com", "password": "nope"}
    )
    assert unknown.json()["detail"] == response.json()["detail"]


async def test_viewers_cannot_write(admin_client):
    created = await admin_client.post(
        "/api/auth/users",
        json={"email": "viewer@example.com", "password": "viewer-password", "role": "viewer"},
    )
    assert created.status_code == 201

    login = await admin_client.post(
        "/api/auth/login", json={"email": "viewer@example.com", "password": "viewer-password"}
    )
    viewer_token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {viewer_token}"}

    assert (await admin_client.get("/api/commands", headers=headers)).status_code == 200
    forbidden = await admin_client.post("/api/commands", json=COMMAND, headers=headers)
    assert forbidden.status_code == 403


async def test_the_last_admin_cannot_be_removed(admin_client):
    await admin_client.post(
        "/api/auth/users",
        json={"email": "second@example.com", "password": "second-password", "role": "viewer"},
    )
    # Deleting the viewer is fine; deleting yourself as the only admin is not.
    users = (await admin_client.get("/api/auth/users")).json()
    me = next(u for u in users if u["email"] == ADMIN_EMAIL)
    assert (await admin_client.delete(f"/api/auth/users/{me['id']}")).status_code == 400


async def test_password_change_requires_the_current_password(admin_client):
    bad = await admin_client.post(
        "/api/auth/password", json={"current_password": "wrong", "new_password": "a-new-password"}
    )
    assert bad.status_code == 400

    good = await admin_client.post(
        "/api/auth/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "a-new-password"},
    )
    assert good.status_code == 204


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


async def test_default_commands_are_seeded(admin_client):
    names = {c["name"] for c in (await admin_client.get("/api/commands")).json()}
    assert {"help", "status", "remind", "tasks"} <= names


async def test_command_crud_round_trip(admin_client):
    created = await admin_client.post("/api/commands", json=COMMAND)
    assert created.status_code == 201
    command_id = created.json()["id"]

    updated = await admin_client.put(
        f"/api/commands/{command_id}", json={"static_response": "pong!", "priority": 7}
    )
    assert updated.json()["static_response"] == "pong!"
    assert updated.json()["priority"] == 7

    assert (await admin_client.delete(f"/api/commands/{command_id}")).status_code == 204
    assert (await admin_client.put(f"/api/commands/{command_id}", json={})).status_code == 404


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({**COMMAND, "static_response": "   "}, "needs a response template"),
        ({**COMMAND, "static_response": "{{ broken"}, "invalid"),
        ({**COMMAND, "trigger_type": "regex", "pattern": "([x"}, "not a valid regular expression"),
        ({**COMMAND, "response_type": "builtin", "builtin_name": "nope"}, "Unknown built-in"),
        ({**COMMAND, "response_type": "integration"}, "needs an integration_id"),
        ({**COMMAND, "response_type": "integration", "integration_id": 999}, "No integration with id"),
    ],
)
async def test_invalid_commands_are_rejected_with_a_reason(admin_client, payload, fragment):
    response = await admin_client.post("/api/commands", json=payload)
    assert response.status_code == 422
    assert fragment in response.json()["detail"]


async def test_duplicate_command_names_conflict(admin_client):
    await admin_client.post("/api/commands", json=COMMAND)
    duplicate = await admin_client.post("/api/commands", json=COMMAND)
    assert duplicate.status_code == 409


async def test_command_test_endpoint_reports_matches(admin_client):
    await admin_client.post("/api/commands", json=COMMAND)
    matched = await admin_client.post(
        "/api/commands/test", json={"text": "/ping", "dry_run": False}
    )
    assert matched.json() == {
        "matched": True, "command_name": "ping", "args": "", "reply": "pong", "error": None,
    }

    unmatched = await admin_client.post("/api/commands/test", json={"text": "???"})
    assert unmatched.json()["matched"] is False


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


async def test_secrets_are_write_only(admin_client):
    before = (await admin_client.get("/api/config")).json()
    assert before["has_auth_token"] is False
    assert "bot_auth_token" not in before

    saved = await admin_client.put("/api/config", json={"bot_auth_token": "s3cret-token"})
    assert saved.json()["has_auth_token"] is True
    assert "s3cret-token" not in saved.text

    cleared = await admin_client.put("/api/config", json={"bot_auth_token": ""})
    assert cleared.json()["has_auth_token"] is False


async def test_omitted_secret_is_left_alone(admin_client):
    await admin_client.put("/api/config", json={"bot_auth_token": "keep-me"})
    response = await admin_client.put("/api/config", json={"greeting": "Hi there"})
    assert response.json()["has_auth_token"] is True
    assert response.json()["greeting"] == "Hi there"


async def test_config_writes_bump_the_version(admin_client):
    first = (await admin_client.put("/api/config", json={"greeting": "a"})).json()
    second = (await admin_client.put("/api/config", json={"greeting": "b"})).json()
    assert second["config_version"] > first["config_version"]


async def test_an_unknown_timezone_is_rejected(admin_client):
    response = await admin_client.put("/api/config", json={"default_timezone": "Mars/Base"})
    assert response.status_code == 422


async def test_the_llm_responder_is_not_selectable_yet(admin_client):
    response = await admin_client.put("/api/config", json={"responder_mode": "llm"})
    assert response.status_code == 422


async def test_status_reports_the_missing_connection(admin_client):
    body = (await admin_client.get("/api/config/status")).json()
    assert body["connected"] is False
    assert body["commands_loaded"] >= 4
    assert body["scheduler_running"] is True


# --------------------------------------------------------------------------- #
# Integrations
# --------------------------------------------------------------------------- #

INTEGRATION = {
    "name": "weather",
    "base_url": "https://api.example.com",
    "method": "GET",
    "path_template": "/w/{{ params.city }}",
    "reply_template": "{{ response.temp }}",
    "params": [{"name": "city", "required": True}],
}


async def test_integration_crud_and_secret_handling(admin_client):
    created = await admin_client.post(
        "/api/integrations", json={**INTEGRATION, "auth_type": "bearer", "auth_secret": "k3y"}
    )
    assert created.status_code == 201
    body = created.json()
    assert body["has_secret"] is True and "k3y" not in created.text
    assert [p["name"] for p in body["params"]] == ["city"]

    updated = await admin_client.put(
        f"/api/integrations/{body['id']}",
        json={"params": [{"name": "city", "required": True}, {"name": "units"}]},
    )
    assert [p["name"] for p in updated.json()["params"]] == ["city", "units"]
    assert updated.json()["has_secret"] is True  # untouched by a params-only write


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({**INTEGRATION, "base_url": "ftp://x/y"}, "must start with http"),
        ({**INTEGRATION, "reply_template": "{{ broken"}, "reply_template is invalid"),
        ({**INTEGRATION, "params": [{"name": "a"}, {"name": "a"}]}, "must be unique"),
        ({**INTEGRATION, "params": [{"name": "a", "greedy": True}, {"name": "b"}]}, "last parameter"),
    ],
)
async def test_invalid_integrations_are_rejected(admin_client, payload, fragment):
    response = await admin_client.post("/api/integrations", json=payload)
    assert response.status_code == 422
    assert fragment in response.json()["detail"]


async def test_an_integration_in_use_cannot_be_deleted(admin_client):
    integration_id = (await admin_client.post("/api/integrations", json=INTEGRATION)).json()["id"]
    await admin_client.post(
        "/api/commands",
        json={"name": "weather", "pattern": "weather", "response_type": "integration",
              "integration_id": integration_id},
    )
    blocked = await admin_client.delete(f"/api/integrations/{integration_id}")
    assert blocked.status_code == 409
    assert "weather" in blocked.json()["detail"]


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #

TASK = {
    "title": "Stand-up",
    "dm_username": "bo",
    "schedule_kind": "daily",
    "time_of_day": "9:5",
    "timezone_name": "Asia/Ho_Chi_Minh",
    "message": "Time for stand-up, {{ user.username }}",
}


async def test_task_crud_normalises_and_schedules(admin_client, app_env):
    created = await admin_client.post("/api/tasks", json=TASK)
    assert created.status_code == 201
    body = created.json()
    assert body["time_of_day"] == "09:05"
    assert body["schedule_description"] == "daily at 09:05 (Asia/Ho_Chi_Minh)"
    assert body["next_run_at"].endswith("Z")  # stored as UTC
    assert "+07" in body["next_run_local"]
    assert app_env.scheduler.job_count == 1

    disabled = await admin_client.put(f"/api/tasks/{body['id']}", json={"enabled": False})
    assert disabled.json()["enabled"] is False
    assert app_env.scheduler.job_count == 0

    assert (await admin_client.delete(f"/api/tasks/{body['id']}")).status_code == 204


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({**TASK, "time_of_day": "99:00"}, "time out of range"),
        ({**TASK, "timezone_name": "Mars/Base"}, "unknown timezone"),
        ({**TASK, "schedule_kind": "weekly", "days_of_week": "funday"}, "unknown day of week"),
        ({**TASK, "schedule_kind": "cron", "cron_expr": "nope"}, "invalid cron"),
        ({**TASK, "schedule_kind": "once", "run_at": None}, "run_at is required"),
        ({**TASK, "message": "   "}, "needs a message"),
        ({**TASK, "message": "{{ broken"}, "message template is invalid"),
        ({**TASK, "dm_username": None}, "deliver to"),
        ({**TASK, "action_type": "integration"}, "needs an integration_id"),
    ],
)
async def test_invalid_tasks_are_rejected(admin_client, payload, fragment):
    response = await admin_client.post("/api/tasks", json=payload)
    assert response.status_code == 422
    assert fragment in response.json()["detail"].lower() or fragment in response.json()["detail"]


async def test_run_now_delivers_and_records_a_run(admin_client, gateway):
    task_id = (await admin_client.post("/api/tasks", json=TASK)).json()["id"]
    run = await admin_client.post(f"/api/tasks/{task_id}/run")
    assert run.status_code == 200
    assert run.json()["status"] == "ok"

    assert gateway.sent[0].text == "Time for stand-up, bo"
    assert gateway.sent[0].room_id == "dm-bo"

    runs = (await admin_client.get(f"/api/tasks/{task_id}/runs")).json()
    assert len(runs) == 1 and runs[0]["status"] == "ok"


async def test_tasks_can_be_filtered_by_owner(admin_client):
    await admin_client.post("/api/tasks", json={**TASK, "owner_username": "bo"})
    await admin_client.post("/api/tasks", json={**TASK, "owner_username": "other"})
    mine = (await admin_client.get("/api/tasks", params={"owner": "bo"})).json()
    assert len(mine) == 1 and mine[0]["owner_username"] == "bo"


# --------------------------------------------------------------------------- #
# Logs & dashboard
# --------------------------------------------------------------------------- #


async def test_dashboard_summarises_the_install(admin_client):
    body = (await admin_client.get("/api/dashboard")).json()
    assert body["commands_total"] >= 4
    assert body["connection"]["connected"] is False
    assert body["recent_messages"] == []


async def test_writes_are_audited(admin_client):
    await admin_client.post("/api/commands", json=COMMAND)
    audit = (await admin_client.get("/api/logs/audit")).json()
    assert any(
        entry["action"] == "create" and entry["entity"] == "command" for entry in audit
    )
    assert audit[0]["actor"] == ADMIN_EMAIL
