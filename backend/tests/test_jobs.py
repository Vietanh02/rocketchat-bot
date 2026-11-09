"""Jobs: work the assistant does in several steps, each a turn of its own."""

from __future__ import annotations

import json

import pytest

from app.agent.jobs import JobError, JobPlan, clean_plan, progress_text
from app.models import AgentJob, AgentJobPhase, JobStatus
from tests.test_agent import model, set_agent, settle  # noqa: F401
from tests.test_llm import tool_use
from tests.test_responder import context, incoming

STEPS = ["Find the leave policy", "Write the answer"]


def jobs_of(app_env):
    return app_env.bot.responder.agent.jobs


@pytest.fixture
def held(app_env):
    """Jobs are created but not run until the test runs them, one at a time."""
    started: list[int] = []
    jobs_of(app_env).start = started.append
    return started


async def run(app_env, job_id: int) -> AgentJob:
    jobs = jobs_of(app_env)
    await jobs._run(job_id)
    return await jobs.get(job_id)


async def snapshot(app_env):
    return await app_env.config_store.current()


async def new_job(app_env, steps=STEPS, **kwargs) -> AgentJob:
    message = kwargs.pop("message", None) or context("Compare the policies")
    return await jobs_of(app_env).create(
        JobPlan("Compare the policies", list(steps)),
        message=message,
        question="Compare the policies",
        thread_id=None,
        session_key="dm:R1",
        session_kind="dm",
        snapshot=await snapshot(app_env),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Plans and the checklist
# --------------------------------------------------------------------------- #


def test_a_plan_is_cleaned_and_kept_short():
    plan = clean_plan("  Report ", ["a", " ", "b", "c", "d"], 3)
    assert plan == JobPlan("Report", ["a", "b", "c"])
    assert clean_plan("x", "not a list", 3) is None
    assert clean_plan("", ["only"], 3).title == "only"


async def test_the_checklist_shows_each_step(app_env, gateway, model):
    job = await new_job(app_env)
    job.phases[0].status = "done"
    job.phases[1].status = "running"
    text = progress_text(job)
    assert f"*Job #{job.id}: Compare the policies*" in text
    assert ":white_check_mark: 1. Find the leave policy" in text
    assert ":hourglass_flowing_sand: 2. Write the answer" in text
    assert f"job cancel {job.id}" in text


# --------------------------------------------------------------------------- #
# Starting a job
# --------------------------------------------------------------------------- #


async def test_the_model_starts_a_job_and_each_step_runs_on_its_own(app_env, gateway, model, held):
    model.answers = [
        tool_use("start_job", title="Compare the policies", steps=STEPS),
        "I am on it as job #1.",
        "Leave: twelve days.",
        "You get **twelve days** of leave.",
    ]
    await app_env.bot._handle(incoming("Compare our leave policies and write a summary"))
    assert gateway.sent[-1].text.startswith("I am on it as job #1.")
    assert "start_job" in [t["function"]["name"] for t in model.requests[0]["tools"]]
    [job_id] = held

    job = await run(app_env, job_id)
    assert job.status == JobStatus.done.value
    assert [p.output for p in job.phases] == ["Leave: twelve days.", "You get **twelve days** of leave."]
    # The second step saw the first one's result, and was told it is the last.
    step_two = model.requests[3]["messages"][-1]["content"]
    assert "### Step 1: Find the leave policy\nLeave: twelve days." in step_two
    assert "Now do step 2: Write the answer" in step_two and "This is the last step" in step_two
    # Steps speak as the bot, like any turn.
    system = model.requests[2]["messages"][0]["content"]
    assert "{bot}" not in system and "one step of a job" in system
    # A step never offers starting another job.
    assert "start_job" not in [t["function"]["name"] for t in model.requests[2].get("tools", [])]

    # The checklist, edited to done, and the answer, formatted for Rocket.Chat.
    checklist = next(m for m in gateway.sent if m.message_id == job.progress_msg_id)
    assert ":white_check_mark: 2. Write the answer" in checklist.text and "Done in" in checklist.text
    assert gateway.sent[-1].text.startswith("You get *twelve days* of leave.")
    assert f"Job #{job_id}" in gateway.sent[-1].text
    # The answer joins the conversation.
    state = await app_env.bot.responder.agent.sessions.open("dm:R1", "dm", context("x"))
    assert state.messages[-1]["content"] == "You get **twelve days** of leave."


async def test_a_turn_that_runs_long_goes_on_as_a_job(app_env, gateway, model, held):
    from tests.test_streaming import set_reply_mode

    await set_reply_mode(app_env, "stream")
    await set_agent(app_env, job_after_seconds=0.3)
    model.token_delay = 0.1
    model.answers = ["one two three four five six seven eight nine ten"]
    model.on_plan = lambda body: json.dumps({"title": "Big report", "steps": ["Gather", "Write"]})
    await app_env.bot._handle(incoming("Write a report on everything"))
    await settle(app_env)
    reply = gateway.sent[-1].text
    assert "job #1 in 2 steps" in reply
    [job_id] = held
    job = await jobs_of(app_env).get(job_id)
    assert job.started_by == "deadline" and [p.goal for p in job.phases] == ["Gather", "Write"]


async def test_without_a_plan_a_long_turn_is_cut_off_as_before(app_env, gateway, model, held):
    from tests.test_streaming import set_reply_mode

    await set_reply_mode(app_env, "stream")
    await set_agent(app_env, job_after_seconds=0.3)
    model.token_delay = 0.1
    model.answers = ["one two three four five six seven eight nine ten"]
    await app_env.bot._handle(incoming("Write a report on everything"))
    assert held == [] and "this is as far as I got" in gateway.sent[-1].text


async def test_a_person_has_a_limited_number_of_jobs(app_env, gateway, model):
    await set_agent(app_env, jobs_per_user=1)
    await new_job(app_env)
    with pytest.raises(JobError, match="already have 1"):
        await new_job(app_env)
    # Someone else may still start one.
    await new_job(app_env, message=context("x", sender_id="U2", sender_username="an"))


async def test_jobs_can_be_turned_off(app_env, gateway, model):
    await set_agent(app_env, jobs_enabled=False)
    model.answers = ["ok"]
    await app_env.bot._handle(incoming("hello there friend"))
    assert "start_job" not in [t["function"]["name"] for t in model.requests[0].get("tools", [])]
    with pytest.raises(JobError):
        await new_job(app_env)


# --------------------------------------------------------------------------- #
# Failing, resuming, stopping
# --------------------------------------------------------------------------- #


async def test_a_step_that_keeps_failing_stops_the_job(app_env, gateway, model):
    job = await new_job(app_env)
    model.hang = True
    job = await run(app_env, job.id)
    assert job.status == JobStatus.failed.value and "step 1 failed" in job.error
    assert job.phases[0].attempts == 2 and job.phases[0].status == "failed"
    assert gateway.sent[-1].text.startswith(f":warning: Job #{job.id} stopped")


async def test_a_restart_goes_on_from_the_step_under_way(app_env, gateway, model, held):
    job = await new_job(app_env)
    async with app_env.session_factory() as db:
        first = await db.get(AgentJobPhase, job.phases[0].id)
        first.status, first.output = "done", "Found it earlier."
        (await db.get(AgentJob, job.id)).status = JobStatus.running.value
        await db.commit()
    assert await jobs_of(app_env).resume() == 1 and held == [job.id]
    model.answers = ["The answer."]
    job = await run(app_env, job.id)
    assert job.status == JobStatus.done.value and len(model.requests) == 1
    assert "Found it earlier." in model.requests[0]["messages"][-1]["content"]


async def test_people_list_follow_and_stop_their_own_jobs(app_env, gateway, model, held):
    job = await new_job(app_env)
    reply = await app_env.bot.responder.respond(context("job list"))
    assert f"#{job.id}" in reply.text and "0/2 steps" in reply.text
    reply = await app_env.bot.responder.respond(context(f"job status {job.id}"))
    assert "1. Find the leave policy" in reply.text
    other = await app_env.bot.responder.respond(context(f"job cancel {job.id}", sender_id="U2"))
    assert "You have no job" in other.text
    reply = await app_env.bot.responder.respond(context(f"job cancel #{job.id}"))
    assert "Stopped job" in reply.text
    assert (await jobs_of(app_env).get(job.id)).status == JobStatus.cancelled.value
    again = await app_env.bot.responder.respond(context(f"job cancel {job.id}"))
    assert "already cancelled" in again.text


async def test_admins_see_and_stop_jobs_on_the_portal(admin_client, app_env, gateway, model, held):
    job = await new_job(app_env)
    [listed] = (await admin_client.get("/api/jobs")).json()
    assert listed["id"] == job.id and [p["goal"] for p in listed["phases"]] == STEPS
    response = await admin_client.post(f"/api/jobs/{job.id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert (await admin_client.post(f"/api/jobs/{job.id}/cancel")).status_code == 409
    settings = (await admin_client.put("/api/agent", json={"job_max_phases": 4})).json()
    assert settings["job_max_phases"] == 4 and settings["jobs_enabled"] is True
