"""Importing whole folders into the knowledge base, such as a Confluence export."""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from app.config import get_settings
from app.knowledge.base import BASE_SCOPE
from app.knowledge.folder import attachment_titles, clean_markdown, from_zip
from tests.test_knowledge import NO_VECTORS, kb_of, make_pdf

HOME = "# MYSPACE\n\nWelcome to the team space.\n"
LEAVE = """---
tags:
  - hr
  - policy
---
[MYSPACE](../MYSPACE.md) > [Human resources](../MYSPACE.md)

# Leave policy

Every employee gets **twelve days** of annual leave. See [the overtime rules](./Overtime.md)
and the signed [Policy scan.pdf](../attachments/abc123.pdf).

![org chart](../attachments/img77.png)
"""
OVERTIME = "# Overtime\n\nOvertime is paid at 150 percent of the hourly wage.\n"


def cme_export(**changes) -> bytes:
    """A small space as confluence-markdown-exporter writes it, zipped."""
    files = {
        "MYSPACE/MYSPACE.md": HOME,
        "MYSPACE/MYSPACE/Leave policy.md": LEAVE,
        "MYSPACE/MYSPACE/Overtime.md": OVERTIME,
        "MYSPACE/MYSPACE/Leave policy.comments.md": "# Comments\n\nNice page!",
        "MYSPACE/attachments/abc123.pdf": make_pdf(["Signed policy: twelve days of leave"]),
        "MYSPACE/attachments/img77.png": b"\x89PNG\r\n",
        "MYSPACE/confluence-lock.json": "{}",
        "../escape.md": "# Outside\n\nNever imported.",
    }
    files.update(changes)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            if body is not None:
                archive.writestr(name, body)
    return buffer.getvalue()


async def upload(admin_client, data: bytes, **form) -> dict:
    fields = {"collection": "MYSPACE", **{k: str(v).lower() for k, v in form.items()}}
    response = await admin_client.post(
        "/api/knowledge/imports/zip",
        files={"file": ("MYSPACE.zip", data, "application/zip")},
        data=fields,
    )
    assert response.status_code == 202, response.text
    return response.json()


async def finished(app_env, admin_client, report: dict) -> dict:
    tasks = list(app_env.bot.responder.agent.imports._tasks)
    if tasks:
        await asyncio.gather(*tasks)
    return (await admin_client.get(f"/api/knowledge/imports/{report['id']}")).json()


# --------------------------------------------------------------------------- #
# Reading the export
# --------------------------------------------------------------------------- #


def test_confluence_markdown_is_read_as_words():
    title, text = clean_markdown(LEAVE)
    assert title == "Leave policy"
    assert text.startswith("Tags: hr, policy\n\nMYSPACE > Human resources")
    assert "the overtime rules" in text and "Overtime.md" not in text
    assert "org chart" not in text and "img77" not in text
    assert clean_markdown("See [[Other page|the other page]] and [[Plain]].")[1] == "See the other page and Plain."


def test_attachments_are_named_by_the_links_to_them():
    names = attachment_titles({"MYSPACE/MYSPACE/Leave policy.md": LEAVE})
    assert names == {"MYSPACE/attachments/abc123.pdf": "Policy scan.pdf", "MYSPACE/attachments/img77.png": "org chart"}


def test_a_zip_loses_its_top_folder_and_paths_that_leave_it():
    archive = zipfile.ZipFile(io.BytesIO(cme_export()))
    paths = [f.path for f in from_zip(archive)]
    assert "MYSPACE/Leave policy.md" in paths and "attachments/abc123.pdf" in paths
    assert not any("escape" in p for p in paths)


# --------------------------------------------------------------------------- #
# Importing and syncing
# --------------------------------------------------------------------------- #


async def test_a_zipped_export_is_imported_as_one_collection(admin_client, app_env):
    report = await finished(app_env, admin_client, await upload(admin_client, cme_export(), attachments=True))
    assert report["status"] == "done", report
    # Three pages and the PDF; the picture, the comments and the lock file are skipped.
    assert (report["added"], report["skipped"], report["failures"]) == (4, 3, [])
    documents = await kb_of(app_env).documents(collection="MYSPACE")
    titles = {d.title: d for d in documents}
    assert set(titles) == {"MYSPACE", "Leave policy", "Overtime", "Policy scan"}
    assert titles["Leave policy"].source_path == "MYSPACE/Leave policy.md" and titles["Leave policy"].source == "folder"
    found = await kb_of(app_env).search("how many days of annual leave", [BASE_SCOPE], embedding=NO_VECTORS)
    assert found and found[0].title in {"Leave policy", "Policy scan"}

    listed = (await admin_client.get("/api/knowledge/imports")).json()
    assert listed["collections"] == [{"name": "MYSPACE", "documents": 4, "passages": listed["collections"][0]["passages"]}]
    by_folder = (await admin_client.get("/api/knowledge/documents?collection=MYSPACE")).json()
    assert len(by_folder) == 4


async def test_importing_again_syncs_the_collection(admin_client, app_env):
    await finished(app_env, admin_client, await upload(admin_client, cme_export()))
    changed = cme_export(
        **{
            "MYSPACE/MYSPACE/Overtime.md": "# Overtime\n\nOvertime is paid at 175 percent now.\n",
            "MYSPACE/MYSPACE/Leave policy.md": None,
            "MYSPACE/MYSPACE/Remote work.md": "# Remote work\n\nTwo days a week from home.\n",
        }
    )
    report = await finished(app_env, admin_client, await upload(admin_client, changed))
    assert (report["added"], report["updated"], report["unchanged"], report["removed"]) == (1, 1, 1, 1)
    titles = sorted(d.title for d in await kb_of(app_env).documents(collection="MYSPACE"))
    assert titles == ["MYSPACE", "Overtime", "Remote work"]
    found = await kb_of(app_env).search("overtime percent", [BASE_SCOPE], embedding=NO_VECTORS)
    assert "175 percent" in found[0].text


async def test_an_import_can_skip_the_models_indexing(admin_client, app_env):
    await finished(app_env, admin_client, await upload(admin_client, cme_export(), model_index=False))
    stages = {d.stage for d in await kb_of(app_env).documents(collection="MYSPACE")}
    assert stages == {"basic"}


async def test_a_folder_copied_to_the_server_is_imported(admin_client, app_env, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "kb_import_dir", str(tmp_path))
    space = tmp_path / "TEAM" / "TEAM"
    space.mkdir(parents=True)
    (space / "Onboarding.md").write_text("# Onboarding\n\nYour laptop is ready on day one.\n")
    listed = (await admin_client.get("/api/knowledge/imports")).json()
    assert listed["folders"] == [{"name": "TEAM", "files": 1}]
    response = await admin_client.post("/api/knowledge/imports/folder", json={"folder": "TEAM"})
    assert response.status_code == 202, response.text
    report = await finished(app_env, admin_client, response.json())
    assert report["collection"] == "TEAM" and report["added"] == 1
    bad = await admin_client.post("/api/knowledge/imports/folder", json={"folder": "../etc"})
    assert bad.status_code == 422


async def test_a_collection_can_be_removed(admin_client, app_env):
    await finished(app_env, admin_client, await upload(admin_client, cme_export()))
    response = await admin_client.delete("/api/knowledge/collections/MYSPACE")
    assert response.status_code == 200 and response.json() == {"removed": 3}
    assert await kb_of(app_env).documents(collection="MYSPACE") == []
    assert (await admin_client.delete("/api/knowledge/collections/MYSPACE")).status_code == 404


async def test_a_broken_zip_is_refused(admin_client):
    response = await admin_client.post(
        "/api/knowledge/imports/zip", files={"file": ("x.zip", b"not a zip", "application/zip")}
    )
    assert response.status_code == 422 and "Cannot read the ZIP" in response.json()["detail"]
