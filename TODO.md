# Việc cần làm tiếp (cập nhật 2026-11-06)

## 0. Trạng thái hiện tại

- Stack đang chạy: Rocket.Chat `:3000`, portal `:8081`, bot đã kết nối với tên `@bot`.
- **Chưa commit gì.** Toàn bộ phần LLM, MCP và sửa Mongo đang nằm trong working tree (`git status`).
- Backend: 398 test pass (`cd backend && .venv/bin/python -m pytest -q`). Portal: `npm run build` sạch.

Đã làm trong ngày:

- **Mongo**
  - Mount đúng `./mongo_data:/data/db`, nên dữ liệu không mất khi tạo lại container.
  - Healthcheck tự `rs.initiate` replica set `rs0` khi chạy lần đầu.
  - Rocket.Chat chờ Mongo healthy mới khởi động.
- **LLM service** (trang portal *Language model*)
  - Trả lời tin nhắn không khớp command nào.
  - Cấu hình lưu trong DB: gateway tương thích OpenAI, API key (mã hoá), model, timeout, system prompt, max tokens, temperature, số tin lịch sử.
  - Có nút test.
- **Chuyển Markdown sang định dạng Rocket.Chat** (`backend/app/bot/rcformat.py`)
  - Chạy được cả khi streaming.
  - Bỏ khối `<think>`.
  - Bảng chuyển thành code block.

## 1. Việc nhỏ, làm trước

- [ ] **Test LLM với key thật.**
  1. Portal → Language model: nhập gateway, key, model, bấm *Send test question*.
  2. Tick *Answer unmatched messages*, bấm Save.
  3. Nhắn cho `@bot` một câu không phải command.
  4. Bật Bot settings → Reply style = Streaming rồi thử lại.
  5. Kiểm tra trực tiếp trong Rocket.Chat: in đậm, in nghiêng, danh sách, bảng, code block.
- [ ] **Commit.** Nên tách 3 commit:
  - Mongo / docker-compose.
  - LLM service + formatter.
  - Phần LLM tool của integration (đã có từ trước, cũng chưa commit).
- [ ] **Tin nhắn dài hơn 5000 ký tự** (giới hạn mặc định `Message_MaxAllowedSize` của Rocket.Chat) đang bị Rocket.Chat từ chối.
  - Cần tự chia thành nhiều tin, cắt ở ranh giới đoạn, không cắt giữa code block.
  - Phải tương thích với streaming: khi gần chạm giới hạn thì chốt tin hiện tại rồi mở tin mới.
- [ ] Quyết định giữ hay xoá `scripts/opencode_ai.py`. Nó không còn cần cho LLM; nếu chọn hướng B bên dưới thì sẽ được thay bằng service.

## 2. Việc chính: cho bot gọi MCP và skill

### Cần trả lời trước khi làm

- [ ] Những MCP server nào? Chạy ở đâu?
  - Hôm nay không tìm thấy MCP nào được cấu hình trên máy này: `~/.claude.json`, Codex, opencode đều trống.
  - Cần biết loại transport: **HTTP** (URL, header) hay **stdio** (lệnh chạy).
- [ ] Những skill nào?
  - Trên máy đang có `notebooklm`: CLI `/usr/local/bin/notebooklm`, thông tin đăng nhập ở `~/.notebooklm`.
  - Và các skill đồng bộ từ claude.ai trong `~/.claude/skills/synced` (docx, pdf, xlsx…).
- [ ] Ai được dùng? Mọi người hay chỉ allowlist? Có tool nào ghi dữ liệu hoặc xoá dữ liệu không?

### Hướng A: backend làm MCP client (khuyên dùng cho MCP)

LLM service hiện có sẽ gọi tool MCP như đang gọi "LLM tool" của integration.

- [ ] **Bảng `mcp_server`:**
  - `name`, `transport` (`http` | `stdio`), `url`, `headers_enc` (mã hoá).
  - `command`, `args`, `env_enc`, `enabled`, `timeout_seconds`.
- [ ] **Bảng `mcp_tool`:** `server_id`, `name`, `description`, `input_schema`, `enabled`.
  - Mặc định là **tắt**: admin bật từng tool, vì người chat có thể khiến LLM gọi tool với tham số tuỳ ý.
- [ ] **Client:** dùng SDK `mcp` (Python), file mới `backend/app/integrations/mcp.py`.
  - Hàm `list_tools()` để nút *Refresh tools* đồng bộ danh sách vào DB.
  - Hàm `call_tool(name, args)` trả về text hoặc JSON.
  - Có timeout và giới hạn độ dài kết quả, tương tự `MAX_TOOL_RESULT_CHARS`.
  - Quyết định giữ session lâu dài hay mở mới mỗi lần gọi. Nên bắt đầu với mở mới mỗi lần, đơn giản hơn.
- [ ] **Toolbox (`backend/app/bot/llm.py`):** gộp tool MCP với tool integration.
  - Đặt tên dạng `server__tool` để không trùng.
  - Lỗi của MCP được trả lại cho model, không ném ra ngoài, giống integration.
- [ ] **API `/api/mcp`:** CRUD server, refresh tools, bật/tắt tool, test gọi một tool. Chỉ admin được sửa.
- [ ] **Portal:** trang *MCP servers* gồm danh sách server, form thêm, nút Refresh tools, bật/tắt từng tool, ô test.
- [ ] **Docker:**
  - MCP dạng HTTP chạy trên host: thêm `extra_hosts: host.docker.internal:host-gateway` cho backend.
  - MCP dạng stdio: image backend cần `node`/`npx` hoặc `uv`/`uvx`, tuỳ server.
  - Áp dụng guard chặn host nội bộ giống integration. Hiện `.env` đang đặt `APP_ALLOW_PRIVATE_INTEGRATION_HOSTS=true`.
- [ ] **Log:** ghi tool nào được gọi và mất bao lâu vào Logs.
- [ ] **Test:** dùng MCP server giả (in-process) cho list, call, lỗi, timeout, tool đang tắt.

### Hướng B: giao cho agent (cách duy nhất chạy skill đúng nghĩa)

Skill gồm `SKILL.md` và script, cần một agent có shell. Bot chuyển câu hỏi cho agent chạy trên host, như `opencode serve` hoặc Claude Code headless. Agent tự đọc skill, gọi MCP, chạy lệnh; bot stream kết quả về chat.

- [ ] **Responder kiểu "agent"** (có thể là command `ai ...` hoặc một chế độ cho tin không khớp).
  - Cấu hình trên portal: URL, mật khẩu, model, thư mục làm việc, timeout.
  - Mỗi user/room một session; lệnh `ai new` để bắt đầu lại.
  - Stream qua sự kiện SSE của agent, dùng `LiveReply` có sẵn.
- [ ] **Bảo mật** (bắt buộc, vì người chat gián tiếp chạy lệnh trên server):
  - Chỉ user trong allowlist riêng được dùng.
  - Agent chạy dưới user Linux riêng, không phải root, trong sandbox hoặc container riêng.
  - Giới hạn tool mà agent được dùng.

### Hướng C: bọc từng skill thành command

- [ ] Ví dụ: command `nlm <câu hỏi>` gọi CLI `notebooklm` qua script command.
- [ ] Phải mount CLI và `~/.notebooklm` vào container backend, hoặc chạy qua HTTP bên ngoài.

**Đề xuất:** làm A trước. Sau đó chọn B (chỉ cho nhóm tin cậy) hoặc C (vài skill cố định).

## 3. Ghi chú

- **Migration:** `create_all` lúc khởi động tự tạo bảng mới nhưng không nâng revision Alembic (DB đang ở `c41f0a9d2e57`). Migration `d5e8b31c7a40` đã được viết để bỏ qua nếu bảng có sẵn. Migration mới sau này nên viết theo cùng cách.
- **`mongo_data`:** phải thuộc uid 1000, user mà mongod chạy dưới.
- **Biến `APP_LLM_*`** trong `.env` chỉ điền sẵn khi tạo DB mới; sau đó portal quyết định.
