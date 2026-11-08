"""How well the knowledge base finds the passage that answers a question.

A Vietnamese employee handbook with tables, and questions of three kinds:
sharing words with the text, saying the same thing in other words, and
looking up one row of a table. Each question names a phrase the answer
needs; a lookup "hits" when that phrase is in a passage the model is shown.

Five set-ups, from words alone to everything:

1. words: headings cut, FTS5 only
2. words + model index: the language model's sections, keywords, questions
3. vectors: headings cut, word and vector search
4. model index + vectors
5. model index + vectors + the model picking the passages

Runs against the real services: the language model gateway configured in
the bot, the embedding server and Qdrant. It uses its own SQLite file and
Qdrant collections (prefix "rcbot_eval_"), removed afterwards.

    docker exec -i rocketchat_bot-backend-1 python - < bench/kb_eval.py
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

LIVE_DB = os.environ.get("EVAL_LIVE_DB", "/app/data/bot.db")
os.environ["APP_DATABASE_URL"] = f"sqlite+aiosqlite:///{Path(tempfile.mkdtemp()) / 'eval.db'}"
os.environ["APP_KB_FILES_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.environ.get("EVAL_APP_DIR", "/app"))

import httpx  # noqa: E402

from app.bot.config_store import EmbeddingSettings, LLMSettings  # noqa: E402
from app.bot.llm import LLMChat  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.knowledge.base import KnowledgeBase  # noqa: E402
from app.knowledge.indexer import judge  # noqa: E402
from app.knowledge.service import KnowledgeService  # noqa: E402
from app.knowledge.vectorstore import QdrantStore  # noqa: E402
from app.security import decrypt_secret  # noqa: E402

HANDBOOK = """# Sổ tay nhân viên Công ty ABC

## 1. Giờ làm việc

Giờ làm việc từ 8:00 đến 17:30, nghỉ trưa từ 12:00 đến 13:30, từ thứ Hai đến thứ Sáu. Nhân viên chấm công bằng thẻ từ ở cửa ra vào.

## 2. Nghỉ phép năm

Mỗi nhân viên chính thức có 12 ngày phép năm. Cứ đủ 5 năm thâm niên được cộng thêm 1 ngày phép.

Ngày phép chưa dùng hết được chuyển sang quý 1 năm sau và hết hạn vào ngày 31/3. Đơn nghỉ phép gửi trên hệ thống HRM ít nhất 3 ngày làm việc trước ngày nghỉ.

## 3. Nghỉ ốm

Nghỉ ốm có giấy xác nhận của bác sĩ được hưởng chế độ BHXH bằng 75% mức lương đóng bảo hiểm. Nghỉ ốm quá 2 ngày phải báo cho quản lý trực tiếp và bộ phận nhân sự.

## 4. Làm thêm giờ

Làm thêm giờ phải được quản lý duyệt trước. Tiền làm thêm giờ tính theo lương giờ như sau:

| Thời điểm | Mức trả |
|---|---|
| Ngày thường | 150% |
| Thứ Bảy, Chủ nhật | 200% |
| Ngày lễ, Tết | 300% |
| Làm ban đêm (22:00 – 6:00) | cộng thêm 30% |

## 5. Làm việc từ xa

Nhân viên được làm việc từ xa tối đa 2 ngày mỗi tuần. Cần đăng ký trên HRM trước 17:00 ngày hôm trước và được quản lý trực tiếp duyệt. Trong giờ làm việc phải luôn liên lạc được qua Rocket.Chat.

## 6. Công tác phí

Chi phí đi công tác được thanh toán theo bảng sau, kèm hoá đơn hợp lệ:

| Khoản | Hà Nội, TP.HCM | Tỉnh, thành khác |
|---|---|---|
| Khách sạn (mỗi đêm) | 800.000đ | 500.000đ |
| Phụ cấp ăn (mỗi ngày) | 250.000đ | 200.000đ |

Vé máy bay đặt hạng phổ thông qua phòng Hành chính. Quyết toán công tác phí trong vòng 10 ngày sau chuyến đi.

## 7. Thiết bị và hỗ trợ CNTT

Mỗi nhân viên được cấp một laptop khi nhận việc. Nếu mất hoặc hỏng thiết bị, phải báo IT trong 24 giờ. Mật khẩu tài khoản công ty phải đổi 90 ngày một lần.

Khi làm việc ngoài văn phòng, truy cập mạng nội bộ bằng VPN FortiClient. Hotline hỗ trợ kỹ thuật: 1900 1234, nhánh 2.

## 8. Bảo mật thông tin

Không chia sẻ mật khẩu cho bất kỳ ai, kể cả đồng nghiệp. Dữ liệu khách hàng được xếp mức "Mật" và chỉ lưu trên máy chủ của công ty. Cổng USB trên máy công ty bị chặn; cần chép dữ liệu thì gửi yêu cầu cho IT.

## 9. Lương và thưởng

Lương được trả vào ngày 5 hàng tháng qua tài khoản ngân hàng. Lương tháng 13 được trả trước Tết Nguyên đán. Việc xét tăng lương diễn ra mỗi năm một lần vào tháng 4.

## 10. Bảo hiểm sức khoẻ

Công ty mua gói bảo hiểm sức khoẻ PVI cho nhân viên chính thức, khám chữa bệnh tại các bệnh viện liên kết. Người thân (bố mẹ, vợ chồng, con) có thể mua thêm với giá 3.500.000đ/năm mỗi người.

## 11. Đào tạo

Ngân sách đào tạo là 10.000.000đ/người/năm. Với khoá học trên 20.000.000đ, nhân viên ký cam kết làm việc tiếp ít nhất 12 tháng sau khi học xong.

## 12. Nghỉ việc

Nhân viên muốn nghỉ việc phải báo trước 30 ngày nếu có hợp đồng xác định thời hạn, 45 ngày nếu hợp đồng không xác định thời hạn. Sau khi bàn giao, công ty quyết toán lương và các khoản khác trong 7 ngày làm việc.

## 13. Danh bạ phòng ban

| Phòng | Trưởng phòng | Email |
|---|---|---|
| Nhân sự | Nguyễn Thị Lan | hr@congty.vn |
| Công nghệ thông tin | Trần Văn Minh | it@congty.vn |
| Kế toán | Lê Thu Hà | ketoan@congty.vn |
| Hành chính | Phạm Quốc Bảo | hanhchinh@congty.vn |
"""

# (question, phrase the answer needs, kind)
QUESTIONS = [
    ("Giờ làm việc buổi sáng bắt đầu lúc mấy giờ?", "8:00 đến 17:30", "words"),
    ("Nghỉ phép năm được bao nhiêu ngày?", "12 ngày phép năm", "words"),
    ("Lương được trả vào ngày nào?", "ngày 5 hàng tháng", "words"),
    ("Ngân sách đào tạo mỗi người là bao nhiêu?", "10.000.000đ/người/năm", "words"),
    ("Quyết toán công tác phí trong bao lâu?", "10 ngày sau chuyến đi", "words"),
    ("Tăng ca vào dịp Tết thì được tính bao nhiêu phần trăm?", "300%", "paraphrase"),
    ("OT thứ Bảy tính thế nào?", "200%", "paraphrase"),
    ("Tôi muốn làm ở nhà thì cần làm gì?", "đăng ký trên HRM", "paraphrase"),
    ("Bị mất máy tính công ty phải làm sao?", "báo IT trong 24 giờ", "paraphrase"),
    ("Bao lâu phải thay password một lần?", "90 ngày", "paraphrase"),
    ("Kết nối vào hệ thống nội bộ khi ở ngoài thì dùng phần mềm gì?", "FortiClient", "paraphrase"),
    ("Tôi muốn thôi việc thì phải báo trước bao lâu?", "30 ngày", "paraphrase"),
    ("Có được cắm ổ cứng di động vào máy không?", "USB", "paraphrase"),
    ("Bố mẹ tôi có được tham gia bảo hiểm của công ty không?", "3.500.000đ/năm", "paraphrase"),
    ("Thưởng Tết trả khi nào?", "tháng 13", "paraphrase"),
    ("Ngày phép không dùng hết có bị mất không?", "31/3", "paraphrase"),
    ("Bị ốm thì được hưởng bao nhiêu lương?", "75%", "paraphrase"),
    ("Làm khuya có được thêm tiền không?", "30%", "paraphrase"),
    ("Khi nào công ty xem xét điều chỉnh thu nhập?", "tháng 4", "paraphrase"),
    ("Học khoá đắt tiền có phải ràng buộc gì không?", "12 tháng", "paraphrase"),
    ("Email phòng kế toán là gì?", "ketoan@congty.vn", "table"),
    ("Ai là trưởng phòng IT?", "Trần Văn Minh", "table"),
    ("Số điện thoại hỗ trợ kỹ thuật là gì?", "1900 1234", "table"),
    ("Đi công tác Đà Nẵng thì khách sạn được bao nhiêu một đêm?", "500.000đ", "table"),
    ("Vé máy bay công tác được đi hạng nào?", "hạng phổ thông", "table"),
    ("Có vấn đề về nhân sự thì liên hệ ai?", "Nguyễn Thị Lan", "table"),
]

TOP_K = 3


def live_settings() -> tuple[LLMSettings, EmbeddingSettings]:
    db = sqlite3.connect(LIVE_DB)
    base_url, key, model = db.execute("select base_url, api_key_enc, model from llm_config").fetchone()
    llm = LLMSettings(base_url=base_url, api_key=decrypt_secret(key), model=model, timeout_seconds=120)
    embedding = EmbeddingSettings(
        provider=os.environ.get("EVAL_EMBED_PROVIDER", "tei"),
        base_url=os.environ.get("EVAL_EMBED_URL", "http://embed:80"),
        model=os.environ.get("EVAL_EMBED_MODEL", ""),
        api_key=os.environ.get("EVAL_EMBED_KEY") or None,
        query_prefix=os.environ.get("EVAL_QUERY_PREFIX", "query: "),
        document_prefix=os.environ.get("EVAL_DOCUMENT_PREFIX", "passage: "),
        timeout_seconds=60,
    )
    return llm, embedding


async def main() -> None:
    llm_settings, embedding = live_settings()
    chat = LLMChat.from_settings(llm_settings)
    assert chat is not None, "the bot's language model is not set up"
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    http = httpx.AsyncClient(timeout=60)
    store = QdrantStore(
        os.environ.get("APP_QDRANT_URL", "http://qdrant:6333"), http=http, prefix="rcbot_eval_"
    )
    knowledge = KnowledgeService(SessionLocal, http_client=http, store=store)
    kb = KnowledgeBase(SessionLocal, knowledge)
    model_name = os.environ.get("EVAL_INDEX_MODEL") or None

    # One copy cut by headings, one indexed by the model.
    snapshot = SimpleNamespace(
        knowledge=SimpleNamespace(llm_index=True, index_model=model_name or ""),
        agent=SimpleNamespace(fast_model=""),
        embedding=embedding,
    )
    kb.model_access = None
    plain, _ = await kb.add("eval-plain", "so-tay.md", HANDBOOK.encode(), embedding=EmbeddingSettings())
    if kb._pipeline_task:
        await kb._pipeline_task

    async def access():
        return snapshot, chat

    kb.model_access = access
    started = time.monotonic()
    indexed, _ = await kb.add("eval-model", "so-tay.md", HANDBOOK.encode(), embedding=EmbeddingSettings())
    while kb._pipeline_task and not kb._pipeline_task.done():
        await kb._pipeline_task
    index_seconds = time.monotonic() - started
    [plain_info] = await kb.documents(scope="eval-plain")
    [indexed_info] = await kb.documents(scope="eval-model")
    print(
        f"heading cut: {plain_info.chunks} passages in {plain_info.sections} sections\n"
        f"model index: {indexed_info.chunks} passages in {indexed_info.sections} sections, "
        f"stage {indexed_info.stage}, {index_seconds:.1f}s{' — ' + indexed_info.stage_error if indexed_info.stage_error else ''}"
    )

    started = time.monotonic()
    embedded = await kb.index_pending(embedding)
    print(f"embedded {embedded} passages (with their questions) in {time.monotonic() - started:.1f}s")

    setups = [
        ("words", "eval-plain", EmbeddingSettings(), False),
        ("words + model index", "eval-model", EmbeddingSettings(), False),
        ("vectors (headings cut)", "eval-plain", embedding, False),
        ("model index + vectors", "eval-model", embedding, False),
        ("model index + vectors + model picks", "eval-model", embedding, True),
    ]
    results = []
    for name, scope, settings, judged in setups:
        hits1 = hits3 = 0
        by_kind: dict[str, list[int]] = {}
        misses = []
        took = []
        shown_chars = []
        for question, needed, kind in QUESTIONS:
            begin = time.monotonic()
            found = await kb.search(question, [scope], embedding=settings, limit=12 if judged else TOP_K, min_score=0.3)
            if judged and found:
                picked = await judge(chat, question, [(p.label, p.shown) for p in found], model=model_name)
                if picked is not None:
                    found = [found[i] for i in picked]
            found = await kb.expand(found[:TOP_K])
            took.append(time.monotonic() - begin)
            texts = [p.shown for p in found]
            first = bool(texts) and needed in texts[0]
            any3 = any(needed in t for t in texts)
            hits1 += first
            hits3 += any3
            by_kind.setdefault(kind, [0, 0])
            by_kind[kind][0] += any3
            by_kind[kind][1] += 1
            shown_chars.append(sum(len(t) for t in texts))
            if not any3:
                misses.append(question)
        results.append((name, hits1, hits3, by_kind, sorted(took)[len(took) // 2], sum(shown_chars) / len(shown_chars), misses))

    total = len(QUESTIONS)
    print(f"\n{total} questions; a hit = the phrase the answer needs is in what the model is shown\n")
    print(f"| Set-up | hit@1 | hit@{TOP_K} | words | paraphrase | table | p50 search | chars shown |")
    print("|---|---|---|---|---|---|---|---|")
    for name, h1, h3, kinds, p50, chars, _ in results:
        cells = " | ".join(f"{kinds[k][0]}/{kinds[k][1]}" for k in ("words", "paraphrase", "table"))
        print(f"| {name} | {h1}/{total} | {h3}/{total} | {cells} | {p50 * 1000:.0f} ms | {chars:.0f} |")
    for name, *_, misses in results:
        if misses:
            print(f"\nMissed by {name}:")
            for question in misses:
                print(f"  - {question}")

    for collection in ("kb_passages",):
        await http.delete(f"{store._base}/collections/rcbot_eval_{collection}")
    await http.aclose()
    await chat.aclose()


asyncio.run(main())
