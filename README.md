# AI Personal Productivity OS

AI Personal Productivity OS là hệ thống lập lịch cá nhân bằng AI. Người dùng nhập công việc, deadline, ghi chú riêng hoặc quét Gmail; hệ thống dùng GPT/Gemini để phân tích nhu cầu, xếp lịch 14 ngày, lưu kế hoạch và đồng bộ dữ liệu lên Qdrant để làm bộ nhớ cá nhân.

## Hệ thống làm gì?

Ứng dụng giúp biến thông tin rời rạc như “học tiếng Anh mỗi tối”, “nấu cơm 3 bữa”, “có email lịch họp” thành lịch trình theo ngày. Mỗi người dùng có một lịch riêng theo `user_id`; khi cập nhật, hệ thống giữ lại các tiêu chí cũ và kết hợp với yêu cầu mới.

## Chức năng chính

- Tạo và chỉnh sửa lịch 14 ngày theo từng người dùng.
- Thêm công việc bằng từng ô riêng, có thời lượng và deadline.
- Hỗ trợ deadline hằng ngày hoặc thời gian cụ thể.
- Cho phép chỉ nhập “Thông tin thêm” để cập nhật lịch.
- Quét Gmail read-only trong vài ngày gần nhất để tìm email liên quan lịch trình.
- Dùng OpenAI, Gemini hoặc chế độ đối chiếu cả hai.
- Tự xếp công việc theo ưu tiên, thời lượng, deadline và ràng buộc cá nhân.
- Lưu lịch vào SQLite, file JSON và Qdrant.
- Có guardrails, test và eval để giảm hallucination của LLM.

## Kiến trúc

```text
app/
  api/            # FastAPI routes
  core/           # cấu hình ứng dụng
  integrations/   # Gmail integration
  llm/            # OpenAI, Gemini, compare provider
  memory/         # Voyage embedding + Qdrant
  reliability/    # guardrails, confidence
  storage/        # SQLite + JSON repository
  web/            # giao diện tiếng Việt
  workflow/       # logic phân tích và xếp lịch
tests/            # unit/integration tests
evals/            # golden-set evaluation
```

## Công nghệ

- FastAPI, Pydantic
- OpenAI API, Gemini API
- Voyage embeddings
- Qdrant vector database
- SQLite + JSON persistence
- Gmail API OAuth read-only
- Docker, pytest, ruff

## Cài đặt

```powershell
python -m venv .personal_schedule
.\.personal_schedule\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
copy .env.example .env
```

Điền API key cần dùng trong `.env`:

```env
LLM_PROVIDER=gemini
OPENAI_API_KEY=
GEMINI_API_KEY=

QDRANT_URL=
QDRANT_API_KEY=
QDRANT_COLLECTION=personal_productivity_memory
QDRANT_VECTOR_SIZE=1024
QDRANT_VECTOR_NAME=dense

VOYAGE_API_KEY=
VOYAGE_MODEL=voyage-3.5
```

## Chạy ứng dụng

```powershell
.\.personal_schedule\Scripts\python.exe -m uvicorn app.main:app --reload
```

Mở giao diện:

```text
http://localhost:8000/
```

API docs:

```text
http://localhost:8000/docs
```

## Chọn LLM

```env
LLM_PROVIDER=openai
LLM_PROVIDER=gemini
LLM_PROVIDER=compare
```

- `openai`: chỉ dùng GPT.
- `gemini`: chỉ dùng Gemini.
- `compare`: gọi cả GPT và Gemini để đối chiếu.

## Gmail

Để quét Gmail:

1. Bật Gmail API trong Google Cloud Console.
2. Tạo OAuth Client loại Desktop app.
3. Lưu file credentials tại:

```text
secrets/gmail_credentials.json
```

Thêm vào `.env`:

```env
GMAIL_CREDENTIALS_PATH=secrets/gmail_credentials.json
GMAIL_TOKEN_PATH=data/gmail_token.json
GMAIL_SCAN_DAYS=3
GMAIL_MAX_RESULTS=50
```

Lần đầu bấm “Quét Gmail và cập nhật lịch”, trình duyệt sẽ mở OAuth để chọn tài khoản Gmail.

## Qdrant

Lịch được lưu cục bộ vào SQLite/JSON. Sau đó payload được embed bằng Voyage và upsert lên Qdrant.

Collection Qdrant nên dùng:

```text
collection: personal_productivity_memory
vector name: dense
vector size: 1024
distance: Cosine
```

## Docker

```powershell
docker compose up --build
```

Hoặc build image thủ công:

```powershell
docker build -t personal-schedule-ai .
docker run --env-file .env -p 8000:8000 personal-schedule-ai
```

## Kiểm tra

```powershell
ruff check .
python -m pytest
python -m evals.run --dataset evals/data/golden.jsonl
```
