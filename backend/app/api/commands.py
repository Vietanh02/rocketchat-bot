"""Command CRUD plus a dry-run tester."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.bot.builtins import builtin_names
from app.bot.config_store import bump_config_version
from app.bot.router import compile_pattern
from app.bot.scripts import ScriptError, list_scripts, resolve_script, scripts_root
from app.bot.types import MessageContext
from app.integrations.templating import validate
from app.models import Command, Integration, MatchMode, ResponseType, TriggerType
from app.schemas import (
    CommandCreate,
    CommandOut,
    CommandTestRequest,
    CommandTestResult,
    CommandUpdate,
    ScriptListOut,
    ScriptOut,
)

router = APIRouter(prefix="/api/commands", tags=["commands"])


async def _validate(session: SessionDep, data: dict, current: Command | None) -> None:
    """Reject configurations the bot would not be able to execute."""

    def value(field: str, default=None):
        if field in data and data[field] is not None:
            return data[field]
        return getattr(current, field, default) if current else default

    trigger_type = value("trigger_type", TriggerType.keyword.value)
    pattern = value("pattern", "")
    if trigger_type == TriggerType.regex.value and compile_pattern(
        pattern, bool(value("case_sensitive", False))
    ) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{pattern!r} is not a valid regular expression",
        )

    response_type = value("response_type", ResponseType.static.value)

    if response_type == ResponseType.static.value:
        template = value("static_response", "") or ""
        if not template.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="A static command needs a response template",
            )
        error = validate(template)
        if error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Response template is invalid: {error}",
            )

    elif response_type == ResponseType.builtin.value:
        name = value("builtin_name")
        if name not in builtin_names():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unknown built-in {name!r}. Available: "
                + ", ".join(builtin_names()),
            )

    elif response_type == ResponseType.script.value:
        name = value("script_name")
        if not name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="A script command needs a script_name",
            )
        if trigger_type == TriggerType.keyword.value and (
            value("match_mode", MatchMode.exact.value) != MatchMode.prefix.value
        ):
            # Only "starts with" leaves the rest of the message for arguments.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="A script command with a keyword trigger must use match_mode "
                "'prefix', so users can add arguments after the keyword",
            )
        try:
            resolve_script(name)
        except ScriptError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc

    elif response_type == ResponseType.integration.value:
        integration_id = value("integration_id")
        if integration_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="An integration command needs an integration_id",
            )
        if await session.get(Integration, integration_id) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"No integration with id {integration_id}",
            )


@router.get("/builtins", response_model=list[str])
async def list_builtins(_: CurrentUser) -> list[str]:
    return builtin_names()


@router.get("/scripts", response_model=ScriptListOut)
async def list_available_scripts(_: CurrentUser) -> ScriptListOut:
    """Files in the scripts folder that a `script` command can run."""
    return ScriptListOut(
        directory=str(scripts_root()),
        scripts=[ScriptOut(name=s.name, runner=s.runner) for s in list_scripts()],
    )


@router.get("", response_model=list[CommandOut])
async def list_commands(_: CurrentUser, session: SessionDep) -> list[Command]:
    return list(
        (
            await session.scalars(
                select(Command).order_by(Command.priority, Command.id)
            )
        ).all()
    )


@router.post("", response_model=CommandOut, status_code=status.HTTP_201_CREATED)
async def create_command(
    payload: CommandCreate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> Command:
    data = payload.model_dump()
    await _validate(session, data, None)
    if await session.scalar(select(Command).where(Command.name == payload.name)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A command named {payload.name!r} already exists",
        )

    command = Command(**data)
    session.add(command)
    await session.flush()
    await bump_config_version(session)
    record_audit(session, admin, "create", "command", command.id, command.name)
    await session.commit()
    await runtime.apply_config_change()
    return command


@router.put("/{command_id}", response_model=CommandOut)
async def update_command(
    command_id: int,
    payload: CommandUpdate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> Command:
    command = await session.get(Command, command_id)
    if command is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such command"
        )

    changes = payload.model_dump(exclude_unset=True)
    await _validate(session, changes, command)

    if "name" in changes and changes["name"] != command.name:
        clash = await session.scalar(
            select(Command).where(
                Command.name == changes["name"], Command.id != command_id
            )
        )
        if clash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A command named {changes['name']!r} already exists",
            )

    for field, new_value in changes.items():
        setattr(command, field, new_value)

    await bump_config_version(session)
    record_audit(
        session, admin, "update", "command", command.id, ", ".join(sorted(changes))
    )
    await session.commit()
    await session.refresh(command)
    await runtime.apply_config_change()
    return command


@router.delete("/{command_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_command(
    command_id: int, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> None:
    command = await session.get(Command, command_id)
    if command is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such command"
        )
    name = command.name
    await session.delete(command)
    await bump_config_version(session)
    record_audit(session, admin, "delete", "command", command_id, name)
    await session.commit()
    await runtime.apply_config_change()


def _target(command) -> str:
    if command.response_type == ResponseType.script.value:
        return command.script_name or "?"
    return str(command.builtin_name or command.integration_id)


@router.post("/test", response_model=CommandTestResult)
async def test_command(
    payload: CommandTestRequest, _: AdminUser, runtime: RuntimeDep
) -> CommandTestResult:
    """Run a message through the live router without posting to Rocket.Chat."""
    snapshot = await runtime.config_store.current()
    match = snapshot.router.match(payload.text)
    if match is None:
        return CommandTestResult(matched=False)

    context = MessageContext(
        message_id="portal-test",
        room_id="portal-test",
        text=payload.text,
        sender_id="portal-test",
        sender_username=payload.username,
        room_type=payload.room_type,
        clean_text=payload.text,
        mentioned_bot=True,
    )

    if payload.dry_run and match.command.response_type != ResponseType.static.value:
        # Built-ins can write rows and integrations make live HTTP calls, so a
        # dry run only reports what would have been dispatched.
        return CommandTestResult(
            matched=True,
            command_name=match.command.name,
            args=match.args_text,
            reply=(
                f"(dry run) would dispatch {match.command.response_type} "
                f"`{_target(match.command)}`"
            ),
        )

    try:
        reply = await runtime.bot.responder.respond(context)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - reported back to the portal
        return CommandTestResult(
            matched=True,
            command_name=match.command.name,
            args=match.args_text,
            error=str(exc),
        )

    if reply is not None and reply.denied:
        return CommandTestResult(
            matched=True,
            command_name=match.command.name,
            args=match.args_text,
            error=f"@{payload.username} is not allowed to use the bot (no reply is sent)",
        )

    return CommandTestResult(
        matched=True,
        command_name=match.command.name,
        args=match.args_text,
        reply=reply.text if reply else None,
        error=reply.error if reply else None,
    )
