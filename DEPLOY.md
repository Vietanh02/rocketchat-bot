# Hướng dẫn triển khai Rocket.Chat bot — phiên bản 0.6.0

Tài liệu này hướng dẫn cài bot lên một máy mới, cấu hình nó, rồi nâng cấp, sao lưu và đóng gói các phiên bản sau.
Phần mô tả tính năng nằm trong [README.md](README.md).

## 1. Thành phần

Hệ thống gồm hai stack Docker Compose chạy trên cùng một máy:

| Stack | Container | Vai trò | RAM khi chạy |
|---|---|---|---|
| `rocketchat_server/` | `rocketchat` | Máy chủ chat (cổng 3000) | ~0.8 GB |
| | `mongodb` | CSDL của Rocket.Chat (replica set `rs0`) | ~0.2–0.3 GB |
| `./` (thư mục gốc) | `backend` | Bot, trợ lý AI, API của portal (FastAPI, SQLite) | ~0.15 GB |
| | `portal` | Giao diện quản trị (nginx, cổng 8081) | ~10 MB |
| | `qdrant` | Vector DB cho memory, knowledge base, cache | ~0.05–0.5 GB |
| | `embed` | Model embedding chạy tại máy (TEI, `multilingual-e5-small`) | ~1.2 GB, đỉnh 1.5 GB khi nạp |

Hai stack nối với nhau qua network Docker `rocketchat_server_default`. Bot gọi Rocket.Chat qua địa chỉ `http://rocketchat:3000`.
Network này mang tên thư mục `rocketchat_server`, nên **đừng đổi tên thư mục đó**.

Mô hình ngôn ngữ (LLM) không chạy tại máy. Bot gọi một gateway tương thích OpenAI, cấu hình trên portal.

## 2. Yêu cầu máy

- Linux x86_64, Docker Engine 24 trở lên, Docker Compose v2 (`docker compose version`).
- **RAM ≥ 4 GB.** Máy 3.8 GB đang chạy đủ cả hai stack, nhưng dư không nhiều.
  Nếu ít RAM hơn, bỏ container `embed` và dùng embedding qua gateway (xem mục 6.3).
- **Đĩa trống ≥ 10 GB:** khoảng 5 GB cho các image, phần còn lại cho dữ liệu và bản sao lưu.
- Có Internet khi khởi động lần đầu:
  - tải image Docker;
  - `embed` tải model từ Hugging Face (~470 MB) vào volume `embed_models`, chỉ lần đầu;
  - gateway LLM.
- Mở cổng **3000** (Rocket.Chat) và **8081** (portal) cho người dùng. Các cổng khác chỉ dùng trong mạng Docker.

## 3. Lấy mã nguồn

Có hai cách. Chọn **A** nếu máy đích truy cập được GitHub; chọn **B** nếu muốn cài từ một gói đã đóng sẵn
(ví dụ máy không build được image).

### Cách A — từ GitHub

```bash
git clone https://github.com/Vietanh02/rocketchat-bot.git
cd rocketchat-bot
git checkout release/v0.6.0
```

### Cách B — từ gói phát hành

Gói `rocketchat-bot-0.6.0.tar` do `deploy/package.sh` tạo ra (xem mục 9). Bên trong có:

```
rocketchat-bot-0.6.0/
  rocketchat-bot-0.6.0-source.tar.gz   mã nguồn (giải nén ra rocketchat-bot/)
  images/backend.tar.gz                image backend
  images/portal.tar.gz                 image portal
  images/qdrant_qdrant_v1.19.2.tar.gz  (khi đóng gói với --with-deps)
  DEPLOY.md  RELEASE.txt  SHA256SUMS
```

```bash
tar -xf rocketchat-bot-0.6.0.tar
cd rocketchat-bot-0.6.0
sha256sum -c SHA256SUMS                       # mọi dòng phải là OK
for f in images/*.tar.gz; do gunzip -c "$f" | docker load; done
sudo tar -xzf rocketchat-bot-0.6.0-source.tar.gz -C /opt   # mã nguồn → /opt/rocketchat-bot
cd /opt/rocketchat-bot
export BOT_VERSION=0.6.0                      # dùng image vừa nạp, không build lại
```

Ở các bước sau, thay `docker compose up -d --build` bằng `docker compose up -d --no-build`.

## 4. Cài Rocket.Chat (bỏ qua nếu đã có)

```bash
cd rocketchat_server
sudo mkdir -p mongo_data && sudo chown 1000:1000 mongo_data   # mongod chạy với uid 1000
# Nếu người dùng truy cập bằng tên miền hoặc IP khác localhost, sửa ROOT_URL trong docker-compose.yml.
docker compose up -d
docker compose ps        # chờ mongodb "healthy" và rocketchat "Up"
```

Healthcheck của `mongodb` tự khởi tạo replica set `rs0` trên thư mục dữ liệu trống. Rocket.Chat chỉ khởi động khi Mongo đã sẵn sàng.

1. Mở `http://<máy>:3000`, làm theo màn hình setup để tạo tài khoản admin và workspace.
   - Nếu màn hình setup cứ mở lại sau khi làm xong (bị kẹt ở bước đăng ký Cloud), đánh dấu là đã xong:
     ```bash
     docker compose exec -T mongodb mongosh --quiet rocketchat --eval \
       'db.rocketchat_settings.updateOne({_id:"Show_Setup_Wizard"},{$set:{value:"completed"}})'
     ```
2. **Tạo tài khoản cho bot:** Administration → Users → New user, ví dụ username `bot`.
   - Gán **cả hai role `user` và `bot`.** Role `bot` có quyền vượt giới hạn tốc độ API.
     Nếu thiếu, Rocket.Chat chỉ cho sửa tin 10 lần mỗi phút, và câu trả lời dạng stream sẽ bị lỗi.
3. Đăng nhập bằng tài khoản bot → My Account → Personal Access Tokens → tạo token.
   Ghi lại **User ID** và **Token**.

## 5. Cài bot

```bash
cd ..                                   # thư mục gốc của repo
cp backend/.env.example backend/.env
```

Sửa `backend/.env`:

| Biến | Giá trị |
|---|---|
| `APP_SECRET_KEY` | Chuỗi ngẫu nhiên dài: `openssl rand -hex 32`. **Giữ cố định mãi mãi và sao lưu riêng.** Mọi khoá lưu trong DB (API key của LLM, token Rocket.Chat, header MCP) được mã hoá bằng nó. Đổi giá trị này thì phải nhập lại toàn bộ các khoá đó. |
| `APP_BOOTSTRAP_ADMIN_EMAIL` / `APP_BOOTSTRAP_ADMIN_PASSWORD` | Tài khoản admin đầu tiên của portal. Chỉ được tạo khi DB chưa có người dùng nào. |
| `APP_CORS_ORIGINS` | Không cần đổi: portal gọi API cùng origin. |
| `APP_ALLOW_PRIVATE_INTEGRATION_HOSTS` | Đặt `true` nếu dùng `embed`, Qdrant nội bộ, hoặc MCP server trên máy host. Các địa chỉ `http://embed:80` và `host.docker.internal` là IP nội bộ, sẽ bị chặn nếu để `false`. Lưu ý: khi bật, integration do admin tạo cũng gọi được vào mạng nội bộ. |

Các biến `APP_RC_*` và `APP_LLM_*` có thể để trống; nhập trên portal tiện hơn.

```bash
docker compose up -d --build            # cách B: docker compose up -d --no-build
docker compose ps
curl -s http://localhost:8081/healthz   # {"status":"ok","version":"0.6.0",...}
docker compose logs -f backend          # Ctrl+C để thoát
```

- Đổi cổng portal: `PORTAL_PORT=8090 docker compose up -d`.
- Lần đầu khởi động, backend tự tạo bảng. Các lần sau, nó tự chạy migration còn thiếu. Không cần lệnh migrate riêng.
- `embed` mất 1–2 phút để tải và nạp model. Đợi log `Ready` (`docker compose logs embed | grep Ready`).

## 6. Cấu hình trên portal

Mở `http://<máy>:8081` và đăng nhập bằng tài khoản bootstrap. Nên làm theo thứ tự dưới đây.

### 6.1 Bot settings — kết nối Rocket.Chat
- REST URL `http://rocketchat:3000`, WebSocket URL `ws://rocketchat:3000/websocket`. Compose đã đặt sẵn hai địa chỉ này.
- Dán User ID và Token của bot → **Test connection** → **Save & reconnect**.
- Chấm ở thanh bên chuyển xanh khi bot đã nghe tin. Nhắn DM `help` cho bot để thử.

### 6.2 Language model
- Gateway URL (đến hết `/v1`), API key, model → **Send test question**.
- Tick **Answer unmatched messages** để model trả lời các tin không khớp command nào.
- **Extra request fields for quick jobs:** với model "suy luận" (DeepSeek, Qwen3, GLM…), nhập trường tắt phần suy nghĩ để các tác vụ phụ (phân loại, chọn passage, lập chỉ mục, tóm tắt) nhanh hơn nhiều lần.
  - vLLM/SGLang: `{"chat_template_kwargs": {"enable_thinking": false, "thinking": false}}`
  - DeepSeek API: `{"thinking": {"type": "disabled"}}`
  - OpenAI: `{"reasoning_effort": "low"}`

### 6.3 Memory → Embedding
- Dùng `embed` tại máy: provider **text-embeddings-inference**, URL `http://embed:80`, query prefix `query: `, document prefix `passage: ` → **Test**.
- Không chạy `embed` (máy ít RAM): xoá service `embed` khỏi `docker-compose.yml`, rồi chọn provider OpenAI-compatible với model embedding của gateway.
- **Reranker:** để **none**, trừ khi có một model reranker thật. Trỏ vào `http://embed:80` sẽ lỗi `HTTP 424`, vì `embed` là model embedding, không phải reranker.

### 6.4 Các trang còn lại
| Trang | Việc cần làm |
|---|---|
| Knowledge base | Tải tài liệu lên (PDF, DOCX, MD, TXT, CSV, HTML…). Mỗi tài liệu qua các trạng thái *waiting → indexing → indexed*. |
| Assistant | Fast model (để trống thì dùng model chính); hạn chót một lượt trả lời; cài đặt Jobs (sau 45s thì chuyển thành job). |
| Access | Ai được dùng bot (mọi người, allowlist hoặc blocklist). **Tools by profile:** danh sách người *trusted* và glob tool cho từng profile. |
| Cache | FAQ ghim; ngưỡng tương đồng; thời hạn của câu trả lời đã lưu. |
| MCP servers | Thêm server → **Refresh tools** → bật từng tool. Tool mới mặc định tắt. |
| Skills | Liệt kê các skill trong thư mục `skills/` và bật/tắt từng cái (xem mục 8.3). |

## 7. Kiểm tra sau khi triển khai

- [ ] `curl http://localhost:8081/healthz` trả `"bot_connected": true` và đúng version.
- [ ] DM `help` cho bot: có danh sách lệnh.
- [ ] Hỏi bot một câu: thấy reaction 👀 trong vòng 1 giây, câu trả lời được gõ dần **trong một tin duy nhất**.
- [ ] Hỏi một câu có trong knowledge base: câu trả lời kèm dòng `Sources: [1] …`.
- [ ] Trang Knowledge base: tài liệu ở trạng thái *indexed*, mục "Search" ghi *by meaning and words*.
- [ ] Nhắn `job list`: bot trả lời (chưa có job nào).
- [ ] Chạy `deploy/backup.sh` thành công (mục 10).

## 8. Vận hành

### 8.1 Xem log
```bash
docker compose logs -f backend           # bot, trợ lý, job
docker compose logs --since 1h embed
```
Trên portal, trang **Logs** liệt kê từng tin nhắn, còn trang **Assistant** có thống kê thời gian trả lời.

### 8.2 MCP server trên máy host
Dùng URL `http://host.docker.internal:<port>/mcp` và đặt `APP_ALLOW_PRIVATE_INTEGRATION_HOSTS=true`.
Image backend chỉ có Python. MCP server kiểu stdio viết bằng Node (`npx …`) cần cài thêm Node vào `backend/Dockerfile`.

### 8.3 Thêm skill
Chép thư mục skill vào `skills/<tên>/` (xem mẫu `skills/_example`), rồi bấm **Look again** trên trang Skills.
Thư mục được mount chỉ đọc. Không cần khởi động lại.

## 9. Đóng gói một phiên bản

1. Tăng số phiên bản ở **cả bốn chỗ**, cùng một giá trị `x.y.z`:
   - `VERSION`
   - `backend/pyproject.toml`
   - `portal/package.json`
   - `backend/app/__init__.py`

   Script đóng gói sẽ dừng nếu bốn chỗ không khớp nhau.
2. Commit, rồi chạy:
   ```bash
   deploy/package.sh               # mã nguồn + image backend và portal  (~105 MB)
   deploy/package.sh --with-deps   # thêm image Qdrant và TEI            (~0.7 GB)
   deploy/package.sh --no-images   # chỉ mã nguồn
   ```
3. Kết quả nằm ở `release/rocketchat-bot-<version>.tar` (thư mục `release/` không được commit).
   Gói được dựng từ commit hiện tại (`git archive HEAD`), nên thay đổi chưa commit sẽ không có trong gói.
4. Đặt tag và đẩy lên GitHub:
   ```bash
   git tag -a v<version> -m "rocketchat-bot <version>"
   git push origin v<version>
   ```

## 10. Sao lưu và khôi phục

**Cần giữ:**
- `backend/.env`, nhất là `APP_SECRET_KEY`;
- dữ liệu của bot: DB SQLite và file tài liệu knowledge base;
- `rocketchat_server/mongo_data/`.

Vector trong Qdrant **không cần** sao lưu: bot tự tạo lại từ DB.

```bash
deploy/backup.sh                      # → ../rocketchat_bot_backups/bot-data-<thời điểm>.tar.gz
```

Script dùng cơ chế backup trực tuyến của SQLite, nên chạy được khi bot đang hoạt động. Có thể đặt cron hằng ngày:
`0 3 * * * cd /đường/dẫn/repo && deploy/backup.sh >> /var/log/bot-backup.log 2>&1`.

Sao lưu Rocket.Chat:
```bash
cd rocketchat_server
docker compose exec -T mongodb mongodump --archive --gzip > ../../rc-$(date +%F).archive.gz
```

**Khôi phục dữ liệu bot:**
```bash
docker compose stop backend
docker compose cp ./khôi-phục/bot.db backend:/app/data/bot.db    # sau khi giải nén bản sao lưu
docker compose cp ./khôi-phục/kb/. backend:/app/data/kb/
docker compose start backend
```
Nếu bản sao lưu cũ hơn phiên bản đang chạy, backend tự chạy các migration còn thiếu khi khởi động.

## 11. Nâng cấp và quay lại bản cũ

```bash
deploy/backup.sh                                   # luôn sao lưu trước
git fetch && git checkout release/v<mới>           # hoặc giải nén gói mới và docker load như mục 3B
docker compose up -d --build                       # gói: BOT_VERSION=<mới> docker compose up -d --no-build
docker compose logs backend | grep -E "Running upgrade|Bot started"
curl -s http://localhost:8081/healthz
```

**Quay lại bản cũ:** migration không có chiều đi xuống tự động. Quay lại bằng cách:
1. khôi phục bản sao lưu DB **đã lấy trước khi nâng cấp**;
2. chạy lại image cũ: `git checkout <bản cũ>` rồi `docker compose up -d --build`, hoặc `BOT_VERSION=<cũ> docker compose up -d --no-build`.

## 12. Sự cố thường gặp

| Hiện tượng | Nguyên nhân và cách xử lý |
|---|---|
| `embed` khởi động lại liên tục, `OOMKilled=true` | Không đủ RAM để nạp model. Cần `mem_limit` ≥ 1.5 GB (đã đặt sẵn) và máy còn đủ RAM trống. Nếu không, dùng embedding qua gateway (6.3). |
| Bot trả lời thành **2 tin**, log có `chat.update … 429` | Tài khoản bot thiếu role `bot` (mục 4, bước 2). Từ bản 0.6.0, bot đã tự xoá tin dở dang, nhưng nên thêm role để stream mượt. |
| Rocket.Chat cứ mở màn hình setup | Xem mục 4, bước 1. |
| Rocket.Chat khởi động lại liên tục, `Topology is closed` | Replica set chưa sẵn sàng. Chờ `mongodb` healthy; kiểm tra quyền `mongo_data` (uid 1000). |
| Tài liệu ở trạng thái *model failed*: "returned no usable sections" | Model suy luận quá lâu. Đặt *Extra request fields for quick jobs* (6.2), rồi bấm **Index again**. |
| Log có `Reranking passages failed … 424` | Reranker trỏ vào `embed`. Đặt reranker về **none** (6.3). |
| Portal không nhận được key đã lưu (không giải mã được) | `APP_SECRET_KEY` đã bị đổi. Đặt lại giá trị cũ, hoặc nhập lại các key trên portal. |
| Tải tài liệu lớn bị lỗi 413 | Portal nhận tối đa 20 file × 20 MB mỗi lần. File lớn hơn thì tách nhỏ. |
| `docker compose build` lỗi DNS (`Temporary failure in name resolution`) | Build bằng mạng của host, rồi khởi động không build lại: `docker build --network=host -t rocketchat-bot-backend:latest ./backend` (làm tương tự cho `./portal`), sau đó `docker compose up -d --no-build`. |

## 13. Bảo mật

- Portal chỉ nên mở trong mạng nội bộ, hoặc đặt sau reverse proxy có HTTPS.
- Tool MCP, việc chạy script của skill và integration ghi dữ liệu đều cho phép người chat khiến bot hành động thay họ.
  Chỉ bật những thứ đó cho profile *trusted* (Access → Tools by profile).
- Mọi thao tác ghi trên portal được lưu vào audit log. Tài khoản `viewer` chỉ xem, không sửa được gì.
- `backend/.env` và thư mục sao lưu chứa khoá: giới hạn quyền đọc (`chmod 600 backend/.env`).
