"""The knowledge base: reading documents, cutting them into passages,
searching them, telling questions from requests, documents sent in chat,
and the portal API."""

from __future__ import annotations

import io
import json
import re

import pytest
from sqlalchemy import select, text

from app.agent.intent import CHAT, COMMAND, LOOKUP, classify, detect
from app.agent.prompt import COMMAND_NOTE
from app.agent.runner import with_sources
from app.bot.config_store import EmbeddingSettings, ensure_knowledge_config
from app.bot.llm import LLMService
from app.knowledge.base import BASE_SCOPE, DocumentError, KnowledgeBase, Passage, conversation_scope
from app.knowledge.chunking import chunk_pages
from app.knowledge.indexer import repair
from app.knowledge.parsers import Page, ParseError, parse
from app.knowledge.service import KnowledgeService
from app.knowledge.vectorstore import MemoryVectorStore
from app.models import BotConfig, KbDocument, KbPassage, KbSource
from app.rocketchat.ddp import RcFile, parse_files, parse_message
from app.bot.types import MessageContext
from tests.test_agent import model, settle  # noqa: F401
from tests.test_llm import FakeModel, fake_transport, set_config, tool_use
from tests.test_memory import FakeEmbeddings, set_embedding
from tests.test_responder import context, incoming

NO_VECTORS = EmbeddingSettings()

LEAVE_POLICY = """# Employee handbook

## Annual leave

Every employee gets twelve days of annual leave per year. Unused days expire in March.

## Overtime

Overtime is paid at 150 percent of the hourly wage, 200 percent on public holidays.
"""


def make_pdf(pages: list[str]) -> bytes:
    """A small PDF with one line of text per page (ASCII only)."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids = []
    for page_text in pages:
        content = f"BT /F1 12 Tf 72 720 Td ({page_text}) Tj ET".encode()
        page_number = len(objects) + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page_number + 1} 0 R "
            f"/Resources << /Font << /F1 {{font}} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        kids.append(page_number)
    font_number = len(objects) + 1
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    objects = [obj.replace(b"{font}", str(font_number).encode()) for obj in objects]

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def make_docx() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("Quy định làm việc", level=1)
    document.add_paragraph("Giờ làm việc từ 8 giờ đến 17 giờ, nghỉ trưa một tiếng.")
    document.add_heading("Làm việc từ xa", level=2)
    document.add_paragraph("Nhân viên được làm việc từ xa hai ngày mỗi tuần.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Phòng"
    table.rows[0].cells[1].text = "Kỹ thuật"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def set_knowledge(runtime, **fields):
    async with runtime.session_factory() as session:
        config = await ensure_knowledge_config(session)
        for name, value in fields.items():
            setattr(config, name, value)
        (await session.get(BotConfig, 1)).config_version += 1
        await session.commit()
    runtime.config_store.invalidate()


def kb_of(runtime) -> KnowledgeBase:
    return runtime.bot.responder.agent.kb


async def add_base(runtime, filename="handbook.md", body=LEAVE_POLICY):
    info, _ = await kb_of(runtime).add(BASE_SCOPE, filename, body.encode(), embedding=NO_VECTORS)
    return info


def question_sent(model) -> str:
    return model.requests[0]["messages"][-1]["content"]


# --------------------------------------------------------------------------- #
# Reading documents
# --------------------------------------------------------------------------- #


def test_pdf_pages_are_kept():
    document = parse("report.pdf", make_pdf(["Revenue grew 12 percent", "Costs fell in March"]))
    assert [(page.number, page.text) for page in document.pages] == [
        (1, "Revenue grew 12 percent"),
        (2, "Costs fell in March"),
    ]
    assert document.title == "report"


def test_word_headings_and_tables_are_kept():
    document = parse("quy-dinh.docx", make_docx())
    body = document.pages[0].text
    assert "# Quy định làm việc" in body and "## Làm việc từ xa" in body
    assert "Phòng | Kỹ thuật" in body


def test_html_loses_its_tags_and_scripts():
    html = b"<html><head><title>x</title><script>alert(1)</script></head><body><h2>Hi</h2><p>Text &amp; more</p></body></html>"
    body = parse("page.html", html).pages[0].text
    assert "alert" not in body and "## Hi" in body and "Text & more" in body


def test_unreadable_files_are_explained():
    with pytest.raises(ParseError, match="not supported"):
        parse("photo.png", b"\x89PNG")
    with pytest.raises(ParseError, match="no text"):
        parse("empty.txt", b"   \n ")
    with pytest.raises(ParseError, match="PDF could not be read"):
        parse("broken.pdf", b"%PDF-1.4 nonsense")


def test_passages_follow_sections_and_keep_the_end():
    pages = parse("handbook.md", (LEAVE_POLICY + "\n\n" + "Filler sentence here. " * 120).encode()).pages
    text, _, sections, spans = chunk_pages(pages)
    titles = [sections[span.section].title for span in spans]
    assert titles[:2] == ["Employee handbook › Annual leave", "Employee handbook › Overtime"]
    # The title has nothing under it of its own; it stays with the first section.
    assert text[spans[0].start : spans[0].end].startswith("# Employee handbook\n\n## Annual leave")
    assert text[spans[-1].start : spans[-1].end].endswith("Filler sentence here.")
    # Pages are remembered.
    _, _, _, spans = chunk_pages([Page("first page. " * 50, 1), Page("second page", 2)])
    assert spans[0].page == 1


SAMPLES = {
    "table.md": "# Lương\n\n| Mã | Phòng | Lương |\n|---|---|---|\n"
    + "\n".join(f"| NV{i:03d} | Phòng {i % 7} | {i * 1_000_000:,} |" for i in range(80))
    + "\n\n## Ghi chú\n\n```yaml\nserver:\n  port: 8080\n\n  hosts: [a]\n```\n\nĐoạn cuối.",
    "long.txt": "Một câu rất dài về quy định. " * 200 + "\n\nĐoạn hai\tcó tab  và  hai dấu cách.",
    "rows.tsv": "Mã\tTên\n" + "\n".join(f"A{i}\tBút bi loại {i}" for i in range(300)),
}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_passage_is_an_exact_slice_of_the_text(name):
    document = parse(name, SAMPLES[name].encode())
    text, blocks, _, spans = chunk_pages(document.pages, tabular=name.endswith(".tsv"))
    assert text == SAMPLES[name].strip("\n")
    for span in spans:
        assert span.end - span.start <= 1400
    # Together the passages hold every non-blank character, in order, once.
    joined = "".join(text[s.start : s.end] for s in spans)
    assert "".join(joined.split()) == "".join(text.split())


def test_table_rows_are_never_cut_and_carry_their_header():
    text, _, _, spans = chunk_pages(parse("table.md", SAMPLES["table.md"].encode()).pages)
    for i in range(80):
        holders = [s for s in spans if f"| NV{i:03d} |" in text[s.start : s.end]]
        assert len(holders) == 1
        line = next(l for l in text[holders[0].start : holders[0].end].split("\n") if l.startswith(f"| NV{i:03d} |"))
        assert line == f"| NV{i:03d} | Phòng {i % 7} | {i * 1_000_000:,} |"
    continued = [s for s in spans if text[s.start : s.end].startswith("| NV")]
    assert continued and all(s.context == "| Mã | Phòng | Lương |\n|---|---|---|" for s in continued)
    code = next(s for s in spans if "```yaml" in text[s.start : s.end])
    assert "  port: 8080\n\n  hosts: [a]" in text[code.start : code.end]


def test_word_tables_stay_where_they_are():
    import docx

    document = docx.Document()
    document.add_heading("Mục A", 1)
    document.add_paragraph("Bảng phí mục A:")
    table = document.add_table(rows=2, cols=2)
    for row, cells in zip(table.rows, [("Loại", "Phí"), ("Phí A", "100.000đ")]):
        row.cells[0].text, row.cells[1].text = cells
    document.add_heading("Mục B", 1)
    document.add_paragraph("Nội dung mục B.")
    buffer = io.BytesIO()
    document.save(buffer)
    text, _, sections, spans = chunk_pages(parse("phi.docx", buffer.getvalue()).pages)
    assert text.index("| Phí A | 100.000đ |") < text.index("# Mục B")
    holder = next(s for s in spans if "100.000đ" in text[s.start : s.end])
    assert sections[holder.section].title == "Mục A"


def test_the_models_sections_are_repaired():
    raw = [
        {"first": 5, "last": 9, "title": "B", "keywords": ["x", "x", "y"]},
        {"first": 1, "last": 3, "title": "A"},
        {"first": 2, "last": 2, "title": "inside A"},
        {"first": 8, "last": 40, "title": "C"},
        {"first": "?", "last": 2},
        "junk",
    ]
    sections = repair(raw, 0, 11)
    assert [(s.first, s.last, s.title) for s in sections] == [(0, 3, "A"), (4, 8, "B"), (9, 11, "C")]
    assert sections[1].keywords == ["x", "y"]
    assert repair("nope", 0, 3) == []


# --------------------------------------------------------------------------- #
# Questions, requests and small talk
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "message, kind",
    [
        ("Nhân viên được nghỉ phép bao nhiêu ngày?", LOOKUP),
        ("quy định làm thêm giờ", LOOKUP),
        ("Tạo tài khoản VPN như thế nào?", LOOKUP),
        ("Hãy cho tôi biết chính sách bảo hiểm", LOOKUP),
        ("tóm tắt tài liệu này", LOOKUP),
        ("What is the leave policy?", LOOKUP),
        ("can you explain the onboarding process", LOOKUP),
        ("tạo task họp lúc 9h sáng mai", COMMAND),
        ("Hãy gửi báo cáo cho @an", COMMAND),
        ("giúp mình kiểm tra server còn sống không", COMMAND),
        ("@bot: dịch đoạn này sang tiếng Anh", COMMAND),
        ("Can you create a reminder for 9am", COMMAND),
        ("deploy the app to staging", COMMAND),
        ("xin chào", CHAT),
        ("cảm ơn bạn!", CHAT),
        ("", CHAT),
    ],
)
def test_the_wording_tells_the_intent(message, kind):
    assert classify(message).kind == kind


def test_unclear_wording_is_looked_up():
    assert classify("mức lương tối thiểu vùng 1") is None


async def test_the_fast_model_settles_unclear_wording():
    fake = FakeModel("COMMAND")
    chat = LLMService(http_client=fake_transport(fake)).chat_for(
        __import__("app.bot.config_store", fromlist=["LLMSettings"]).LLMSettings(
            base_url="http://llm.invalid/v1", api_key="k", model="main"
        )
    )
    intent = await detect("mức lương tối thiểu vùng 1", mode="model", chat=chat, fast_model="fast")
    assert (intent.kind, intent.by) == (COMMAND, "model")
    assert fake.requests[0]["model"] == "fast" and fake.requests[0]["max_tokens"] == 5
    # Clear wording never asks the model.
    await detect("xin chào", mode="model", chat=chat)
    assert len(fake.requests) == 1
    # Without the model mode, unclear wording is a lookup.
    assert (await detect("mức lương tối thiểu vùng 1")).by == "default"


# --------------------------------------------------------------------------- #
# The knowledge base
# --------------------------------------------------------------------------- #


async def test_documents_are_searched_by_their_words(app_env):
    kb = kb_of(app_env)
    info = await add_base(app_env)
    assert info.chunks == 2 and info.pages is None
    found = await kb.search("how many days of annual leave?", [BASE_SCOPE], embedding=NO_VECTORS)
    assert found[0].label == "handbook — Employee handbook › Annual leave"
    # Question words alone match nothing in particular.
    assert await kb.search("what is it?", [BASE_SCOPE], embedding=NO_VECTORS) == []


async def test_the_same_file_is_not_added_twice(app_env):
    kb = kb_of(app_env)
    first = await add_base(app_env)
    again, created = await kb.add(BASE_SCOPE, "copy.md", LEAVE_POLICY.encode(), embedding=NO_VECTORS)
    assert not created and again.id == first.id
    # Another conversation may have it.
    _, created = await kb.add("conv:dm:R1", "copy.md", LEAVE_POLICY.encode(), embedding=NO_VECTORS)
    assert created


async def test_deleting_a_document_removes_its_passages(app_env):
    kb = kb_of(app_env)
    info = await add_base(app_env)
    assert await kb.delete(info.id) is not None
    async with app_env.session_factory() as db:
        assert (await db.scalars(select(KbPassage))).all() == []
        assert (await db.scalars(select(KbSource))).all() == []
        fts = (await db.execute(text("SELECT count(*) FROM kb_passage_fts WHERE kb_passage_fts MATCH 'leave'"))).scalar()
    assert fts == 0
    assert await kb.search("annual leave", [BASE_SCOPE], embedding=NO_VECTORS) == []


async def test_a_conversation_reads_its_own_documents_first(app_env):
    kb = kb_of(app_env)
    await add_base(app_env)
    mine = conversation_scope("dm:R1")
    await kb.add(mine, "contract.md", b"# Contract\n\nThe contractor gets 20 days of annual leave.", embedding=NO_VECTORS)
    lookup = await kb.lookup("annual leave days", mine, embedding=NO_VECTORS, limit=3, min_score=0.3)
    assert lookup.source == "conversation" and lookup.passages[0].title == "contract"
    assert lookup.available == {mine, BASE_SCOPE}
    # Nothing about overtime in the conversation's document: the base answers.
    lookup = await kb.lookup("overtime pay", mine, embedding=NO_VECTORS, limit=3, min_score=0.3)
    assert lookup.source == "base" and lookup.passages[0].section.endswith("Overtime")
    # Other conversations never see it.
    lookup = await kb.lookup("contractor", "conv:dm:R2", embedding=NO_VECTORS, limit=3, min_score=0.3)
    assert lookup.passages == []


async def test_a_conversation_holds_a_limited_number_of_documents(app_env, monkeypatch):
    from app.knowledge import base

    monkeypatch.setattr(base, "MAX_CONVERSATION_DOCUMENTS", 1)
    kb = kb_of(app_env)
    await kb.add("conv:dm:R1", "a.txt", b"first", embedding=NO_VECTORS)
    with pytest.raises(DocumentError, match="already has 1"):
        await kb.add("conv:dm:R1", "b.txt", b"second", embedding=NO_VECTORS)


async def test_passages_get_vectors_and_are_found_by_meaning(app_env):
    server = FakeEmbeddings()
    import httpx

    store = MemoryVectorStore()
    knowledge = KnowledgeService(
        app_env.session_factory, http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)), store=store
    )
    kb = KnowledgeBase(app_env.session_factory, knowledge)
    await set_embedding(app_env, provider="openai", base_url="http://embed.invalid/v1", api_key="k", model="e")
    embedding = (await app_env.config_store.current()).embedding
    await kb.add(BASE_SCOPE, "pets.md", b"# Pets\n\nA kitten may come to the office on Fridays.", embedding=embedding)
    await kb.add(BASE_SCOPE, "drinks.md", b"# Drinks\n\nThe kitchen serves espresso all day.", embedding=embedding)
    await kb._index_task
    assert await store.count("kb_passages") == 2 and await kb.pending_count(embedding) == 0
    found = await kb.search("bring my cats", [BASE_SCOPE], embedding=embedding, limit=1)
    assert found[0].title == "pets"


def test_sources_list_what_the_answer_cites():
    passages = [Passage(1, 1, BASE_SCOPE, "handbook", "x", "Annual leave"), Passage(2, 2, BASE_SCOPE, "report", "y", page=3)]
    assert with_sources("Twelve days [1].", passages) == (
        "Twelve days [1].\n\n_Sources: [1] handbook — Annual leave_"
    )
    assert with_sources("See [2] and [1], not [7].", passages).endswith(
        "[1] handbook — Annual leave · [2] report, page 3_"
    )
    assert with_sources("No citation.", passages) == "No citation."


# --------------------------------------------------------------------------- #
# The assistant looking things up
# --------------------------------------------------------------------------- #


async def test_a_question_is_answered_from_the_knowledge_base(app_env, gateway, model):
    await add_base(app_env)
    model.answers = ["Twelve days per year [1]."]
    await app_env.bot._handle(incoming("How many days of annual leave do I get?"))
    reply = gateway.sent[-1]
    sent = question_sent(model)
    assert "[Passages from the knowledge base" in sent and "[1] handbook — Employee handbook › Annual leave" in sent
    assert sent.endswith("How many days of annual leave do I get?")
    assert "kb_search" in [t["function"]["name"] for t in model.requests[0]["tools"]]
    assert "Sources: [1] handbook — Employee handbook › Annual leave" in reply.text
    # The transcript keeps the model's own words.
    state = await app_env.bot.responder.agent.sessions.open("dm:R1", "dm", context("x"))
    assert state.messages[-1]["content"] == "Twelve days per year [1]."
    from tests.test_agent import runs

    [run] = await runs(app_env)
    labels = json.loads(run.timings_json)["labels"]
    assert labels["intent"] == LOOKUP and labels["kb"] == "base"


async def test_a_request_is_not_looked_up(app_env, gateway, model):
    await add_base(app_env)
    await app_env.bot.responder.respond(context("tạo task nghỉ phép annual leave cho tôi"))
    sent = question_sent(model)
    assert "[Passages" not in sent and COMMAND_NOTE in sent


async def test_small_talk_gets_nothing_extra(app_env, gateway, model):
    await add_base(app_env)
    await app_env.bot.responder.respond(context("xin chào"))
    assert question_sent(model) == "xin chào"


async def test_the_knowledge_base_can_be_turned_off(app_env, gateway, model):
    await add_base(app_env)
    await set_knowledge(app_env, enabled=False)
    await app_env.bot.responder.respond(context("How many days of annual leave?"))
    assert "[Passages" not in question_sent(model)
    assert "kb_search" not in [t["function"]["name"] for t in model.requests[0].get("tools", [])]


async def test_the_model_can_search_further(app_env, gateway, model):
    await add_base(app_env)
    model.answers = [tool_use("kb_search", query="overtime holidays"), "200 percent [1]."]
    await app_env.bot.responder.respond(context("tạo giúp tôi bảng lương ngày lễ"))
    result = json.loads(model.requests[1]["messages"][-1]["content"])
    assert result["passages"][0]["label"] == "handbook — Employee handbook › Overtime"
    assert result["passages"][0]["from"] == "knowledge_base"


# --------------------------------------------------------------------------- #
# Documents sent in chat
# --------------------------------------------------------------------------- #


def with_file(text: str, name: str, data: bytes, gateway, *, type_: str = "", **kwargs) -> MessageContext:
    url = f"/file-upload/F-{name}/{name}"
    gateway.files[url] = data
    message = incoming(text, files=(RcFile(f"F-{name}", name, type_, len(data), url),), **kwargs)
    return MessageContext.from_incoming(message, bot_username="taskbot")


async def test_a_document_sent_alone_is_read_and_acknowledged(app_env, gateway, model):
    reply = await app_env.bot.responder.respond(
        with_file("", "report.pdf", make_pdf(["Revenue grew 12 percent", "Costs fell in March"]), gateway)
    )
    assert "Read *report.pdf*: 2 pages, 1 passages" in reply.text
    assert model.requests == []
    [document] = await kb_of(app_env).documents(source="chat")
    assert document.scope == "conv:dm:R1" and document.uploaded_by == "bo"

    # Questions in this conversation are answered from it.
    model.answers = ["It grew 12 percent [1]."]
    reply = await app_env.bot.responder.respond(context("how much did revenue grow?"))
    assert "documents sent in this conversation" in question_sent(model)
    assert "Sources: [1] report, page 1" in reply.text


async def test_a_question_sent_with_a_document_is_about_it(app_env, gateway, model):
    await add_base(app_env)
    model.answers = ["Summary [1]."]
    await app_env.bot.responder.respond(
        with_file("tóm lại giúp mình", "notes.txt", "Doanh thu quý ba tăng mạnh.".encode(), gateway)
    )
    sent = question_sent(model)
    assert "documents sent in this conversation" in sent and "Doanh thu quý ba" in sent


async def test_a_bad_document_is_reported(app_env, gateway, model):
    reply = await app_env.bot.responder.respond(with_file("", "broken.pdf", b"%PDF-1.4 junk", gateway))
    assert "Could not read *broken.pdf*" in reply.text and reply.error
    model.answers = ["Hello."]
    reply = await app_env.bot.responder.respond(with_file("hi there", "broken.pdf", b"%PDF-1.4 junk", gateway))
    assert reply.text.startswith("Hello.") and "Could not read *broken.pdf*" in reply.text


async def test_pictures_are_not_documents(app_env, gateway, model):
    assert await app_env.bot.responder.respond(with_file("", "cat.png", b"\x89PNG", gateway, type_="image/png")) is None


async def test_new_forgets_the_documents(app_env, gateway, model):
    await app_env.bot.responder.respond(with_file("", "notes.txt", b"Secret plan for Q3 launch", gateway))
    await app_env.bot.responder.respond(context("new"))
    assert await kb_of(app_env).documents(source="chat") == []


async def test_chat_documents_can_be_turned_off(app_env, gateway, model):
    await set_knowledge(app_env, chat_uploads=False)
    assert await app_env.bot.responder.respond(with_file("", "notes.txt", b"plan", gateway)) is None


async def test_documents_in_a_channel_need_a_mention(app_env, gateway, model):
    await set_config(app_env, respond_in_channels=True)
    message = with_file("", "notes.txt", b"plan", gateway, room_type="c", room_id="C1")
    assert await app_env.bot.responder.respond(message) is None


def test_files_are_read_off_messages():
    message = parse_message(
        [
            {
                "_id": "m1",
                "rid": "r1",
                "msg": "",
                "u": {"_id": "u", "username": "bo"},
                "files": [{"_id": "f1", "name": "Quy định.pdf", "type": "application/pdf", "size": 9}],
                "attachments": [{"title": "Quy định.pdf", "title_link": "/file-upload/f1/Quy%20dinh.pdf"}],
            }
        ]
    )
    assert message.files == (RcFile("f1", "Quy định.pdf", "application/pdf", 9, "/file-upload/f1/Quy%20dinh.pdf"),)
    assert parse_files({"file": {"_id": "f2", "name": "a b.txt"}})[0].url == "/file-upload/f2/a%20b.txt"


async def test_a_message_with_only_a_file_is_handled(app_env):
    message = incoming("", files=(RcFile("f", "a.txt", "", 1, "/file-upload/f/a.txt"),), message_id="M9")
    assert app_env.bot._is_actionable(message)
    assert not app_env.bot._is_actionable(incoming("", message_id="M10"))


# --------------------------------------------------------------------------- #
# Portal API
# --------------------------------------------------------------------------- #


async def test_uploading_to_the_knowledge_base(admin_client, app_env):
    response = await admin_client.post(
        "/api/knowledge/documents",
        files=[
            ("files", ("handbook.md", LEAVE_POLICY.encode(), "text/markdown")),
            ("files", ("report.pdf", make_pdf(["Revenue grew"]), "application/pdf")),
            ("files", ("photo.png", b"\x89PNG", "image/png")),
            ("files", ("handbook-copy.md", LEAVE_POLICY.encode(), "text/markdown")),
        ],
    )
    assert response.status_code == 200, response.text
    results = response.json()
    assert [r["ok"] for r in results] == [True, True, False, False]
    assert "2 passages" in results[0]["message"] and "from 1 pages" in results[1]["message"]
    assert "Already in the knowledge base" in results[3]["message"]

    documents = (await admin_client.get("/api/knowledge/documents", params={"source": "portal"})).json()
    assert {d["filename"] for d in documents} == {"handbook.md", "report.pdf"}
    status = (await admin_client.get("/api/knowledge/status")).json()
    assert status["documents"] == 2 and status["semantic"] is False

    found = (await admin_client.get("/api/knowledge/search", params={"q": "overtime pay?"})).json()
    assert found["intent"] == LOOKUP and found["passages"][0]["label"] == "handbook — Employee handbook › Overtime"

    doc_id = documents[0]["id"]
    assert (await admin_client.delete(f"/api/knowledge/documents/{doc_id}")).status_code == 204
    assert (await admin_client.delete(f"/api/knowledge/documents/{doc_id}")).status_code == 404


async def test_knowledge_settings(admin_client):
    response = await admin_client.put("/api/knowledge/config", json={"top_k": 3, "intent_mode": "model"})
    assert response.json() == {
        "enabled": True, "chat_uploads": True, "top_k": 3, "min_score": 0.3, "intent_mode": "model",
        "llm_index": True, "llm_judge": True, "index_model": "",
    }
    assert (await admin_client.put("/api/knowledge/config", json={"top_k": 99})).status_code == 422


async def test_viewers_cannot_upload(client, app_env):
    from app.models import PortalUser, Role
    from app.security import hash_password

    async with app_env.session_factory() as session:
        session.add(PortalUser(email="v@example.com", password_hash=hash_password("pw-123456"), role=Role.viewer.value))
        await session.commit()
    login = await client.post("/api/auth/login", json={"email": "v@example.com", "password": "pw-123456"})
    client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    response = await client.post("/api/knowledge/documents", files=[("files", ("a.md", b"x", "text/markdown"))])
    assert response.status_code == 403
    assert (await client.get("/api/knowledge/documents")).status_code == 200


async def test_asking_about_the_document_gets_its_beginning(app_env, gateway, model):
    await add_base(app_env)
    await app_env.bot.responder.respond(with_file("", "notes.txt", "Doanh thu quý ba tăng mạnh.".encode(), gateway))
    model.answers = ["Tóm tắt [1]."]
    await app_env.bot.responder.respond(context("tóm tắt tài liệu này"))
    sent = question_sent(model)
    assert "documents sent in this conversation" in sent and "Doanh thu quý ba" in sent
    assert "Annual leave" not in sent



# --------------------------------------------------------------------------- #
# The language model indexing documents and choosing passages
# --------------------------------------------------------------------------- #

HANDBOOK = """# Sổ tay nhân viên

## Giờ làm việc

Giờ làm việc từ 8 giờ đến 17 giờ, từ thứ Hai đến thứ Sáu.

## Làm thêm giờ

Làm thêm giờ ngày thường được trả 150% lương giờ.

Làm thêm vào ngày lễ được trả 300% lương giờ.
"""


def sections_answer(body: dict) -> str:
    """What a model indexing HANDBOOK would answer (blocks: 1 title, 2–3, 4–6)."""
    return json.dumps(
        {
            "sections": [
                {"first": 1, "last": 3, "title": "Giờ làm việc", "summary": "Giờ làm việc trong tuần.",
                 "keywords": ["giờ hành chính", "office hours"], "questions": ["Mấy giờ thì vào làm?"]},
                {"first": 4, "last": 6, "title": "Làm thêm giờ", "summary": "Tiền làm thêm giờ.",
                 "keywords": ["tăng ca", "OT", "lương ngoài giờ"],
                 "questions": ["Tăng ca được trả bao nhiêu?", "OT ngày lễ tính thế nào?"],
                 # Whatever the model writes, the passage text stays the document's.
                 "text": "Làm thêm giờ được trả 999%"},
            ]
        },
        ensure_ascii=False,
    )


async def wait_for_pipeline(app_env) -> None:
    kb = kb_of(app_env)
    for _ in range(5):
        if kb._pipeline_task is not None:
            await kb._pipeline_task
        if kb._index_task is not None:
            await kb._index_task


async def test_the_model_indexes_a_document_without_touching_its_text(app_env, gateway, model):
    model.on_index = sections_answer
    info = await add_base(app_env, "so-tay.md", HANDBOOK)
    await wait_for_pipeline(app_env)
    async with app_env.session_factory() as db:
        source = await db.get(KbSource, info.id)
        passages = (await db.scalars(select(KbPassage).order_by(KbPassage.ord))).all()
    assert source.stage == "indexed" and source.fallback_windows == 0
    assert [p.section for p in passages] == ["Giờ làm việc", "Làm thêm giờ"]
    assert "tăng ca" in passages[1].keywords and "Tăng ca được trả bao nhiêu?" in passages[1].questions
    for passage in passages:
        assert passage.text == source.text[passage.start : passage.end]
    assert "999%" not in "".join(p.text for p in passages)
    # The model was shown the numbered blocks, never asked to write them.
    shown = model.side_requests[0]["messages"][1]["content"]
    assert "[5] (paragraph)\nLàm thêm giờ ngày thường" in shown

    # Its keywords let other words find the section.
    found = await kb_of(app_env).search("lương ngoài giờ", [BASE_SCOPE], embedding=NO_VECTORS, limit=1)
    assert found[0].section == "Làm thêm giờ"
    [document] = await kb_of(app_env).documents()
    assert (document.stage, document.sections) == ("indexed", 2)


async def test_a_model_that_cannot_index_leaves_the_heading_cut(app_env, gateway, model):
    model.on_index = lambda body: "Sorry, I cannot."
    info = await add_base(app_env, "so-tay.md", HANDBOOK)
    await wait_for_pipeline(app_env)
    [document] = await kb_of(app_env).documents()
    assert document.stage == "failed" and "no usable sections" in document.stage_error
    found = await kb_of(app_env).search("làm thêm ngày lễ", [BASE_SCOPE], embedding=NO_VECTORS)
    assert found and found[0].document_id == info.id


async def test_indexing_can_be_turned_off(app_env, gateway, model):
    await set_knowledge(app_env, llm_index=False)
    await add_base(app_env, "so-tay.md", HANDBOOK)
    await wait_for_pipeline(app_env)
    assert model.side_requests == []
    [document] = await kb_of(app_env).documents()
    assert document.stage == "basic"


async def test_the_model_chooses_the_passages_that_answer(app_env, gateway, model):
    await add_base(app_env)
    await add_base(app_env, "so-tay.md", HANDBOOK)
    await wait_for_pipeline(app_env)

    def pick_overtime(body):
        listing = body["messages"][1]["content"]
        numbers = re.findall(r"^\[(\d+)\] (.+)$", listing, flags=re.M)
        return json.dumps({"passages": [int(n) for n, label in numbers if "Làm thêm" in label]})

    model.on_judge = pick_overtime
    model.answers = ["300% [1]."]
    await app_env.bot.responder.respond(context("Làm thêm giờ ngày lễ được trả bao nhiêu?"))
    sent = question_sent(model)
    assert "300% lương giờ" in sent and "Annual leave" not in sent
    # The passage comes with its neighbour in the same section, quoted exactly.
    assert "Làm thêm giờ ngày thường được trả 150% lương giờ.\n\nLàm thêm vào ngày lễ" in sent


async def test_when_the_model_finds_nothing_in_the_chats_documents_the_base_answers(app_env, gateway, model):
    await add_base(app_env)
    await app_env.bot.responder.respond(with_file("", "notes.txt", b"Annual party is in December.", gateway))
    model.on_judge = lambda body: (
        '{"passages": []}' if "notes" in body["messages"][1]["content"] else '{"passages": [1]}'
    )
    await app_env.bot.responder.respond(context("how many days of annual leave?"))
    sent = question_sent(model)
    assert "[Passages from the knowledge base" in sent and "twelve days" in sent


async def test_questions_get_vectors_of_their_own(app_env, gateway, model):
    import httpx

    server = FakeEmbeddings()
    store = MemoryVectorStore()
    agent = app_env.bot.responder.agent
    agent.knowledge = KnowledgeService(
        app_env.session_factory, http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)), store=store
    )
    agent.kb.knowledge = agent.knowledge
    await set_embedding(app_env, provider="tei", base_url="http://tei.invalid", query_prefix="query: ", document_prefix="passage: ")
    model.on_index = lambda body: json.dumps(
        {"sections": [{"first": 1, "last": 2, "title": "Pets", "questions": ["Can I bring my cat?", "Is a kitten allowed?"]}]}
    )
    await add_base(app_env, "rules.md", "# Office rules\n\nAnimals must stay outside the building.")
    await wait_for_pipeline(app_env)
    assert await store.count("kb_passages") == 3  # the passage and its two questions
    embedded = server.embedded
    assert "query: Can I bring my cat?" in embedded
    assert any(t.startswith("passage: rules — Pets\n# Office rules") for t in embedded)
    embedding = (await app_env.config_store.current()).embedding
    found = await agent.kb.search("kitten", [BASE_SCOPE], embedding=embedding, limit=1)
    assert found[0].section == "Pets"


async def test_the_uploaded_file_is_kept_and_removed_with_its_document(app_env):
    info = await add_base(app_env)
    async with app_env.session_factory() as db:
        source = await db.get(KbSource, info.id)
    from pathlib import Path

    path = Path(source.file_path)
    assert path.read_bytes() == LEAVE_POLICY.encode()
    await kb_of(app_env).delete(info.id)
    assert not path.exists()


async def test_a_document_can_be_indexed_again(admin_client, app_env):
    await add_base(app_env)
    await wait_for_pipeline(app_env)
    [document] = (await admin_client.get("/api/knowledge/documents")).json()
    response = await admin_client.post(f"/api/knowledge/documents/{document['id']}/reprocess")
    assert response.status_code == 200 and response.json()["stage"] in ("queued", "basic")
    status = (await admin_client.get("/api/knowledge/status")).json()
    assert set(status["stages"]) <= {"queued", "indexing", "basic"}
