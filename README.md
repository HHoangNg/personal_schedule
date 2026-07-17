# AI Personal Productivity OS

AI Personal Productivity OS is an AI-powered personal scheduling system. Users enter tasks, deadlines, personal notes or scan Gmail; the system uses GPT/Gemini to analyze needs, build a 14-day schedule, save plans and synchronize data to Qdrant as personal memory.

## What does the system do?

The application turns scattered information such as “study English every evening”, “cook three meals”, or “there is a meeting scheduled by email” into a daily schedule. Each user has one schedule identified by `user_id`; updates preserve previous criteria and combine them with new requests.

## Main features

- Create and edit a 14-day schedule for each user.
- Add tasks in separate fields with duration and deadline.
- Support daily deadlines or specific date-and-time deadlines.
- Update the schedule using only the “Additional information” field.
- Scan Gmail in read-only mode for recent schedule-related emails.
- Use OpenAI, Gemini or a comparison mode using both providers.
- Automatically schedule tasks according to priority, duration, deadlines and personal constraints.
- Store schedules in SQLite, JSON files and Qdrant.
- Record real execution for each block: completed, skipped, rescheduled or partial.
- Collect daily feedback, performance statistics and a user-specific behavior profile.
- Use real history to provide priority evidence, effective periods, duration estimates and task-splitting suggestions.
- Include guardrails, tests and evaluations to reduce LLM hallucinations.

## Personalization loop

```text
AI proposes a schedule → the user records actual status → daily feedback
→ BehaviorProfile → AI reevaluates priorities and periods at the next update
```

After at least 7 feedback days or 5 execution records, the system can use behavior data
to automatically adjust the schedule. Before that threshold, the data is used only for
statistics and supporting evidence.

## Architecture

```text
app/
  api/            # FastAPI routes
  core/           # application configuration
  integrations/   # Gmail integration
  llm/            # OpenAI, Gemini and comparison provider
  memory/         # Voyage embeddings + Qdrant
  reliability/    # guardrails and confidence
  storage/        # SQLite + JSON repository
  web/            # Vietnamese web interface
  workflow/       # analysis and scheduling logic
tests/            # unit and integration tests
evals/            # golden-set evaluation
```

## Technologies

- FastAPI, Pydantic
- OpenAI API, Gemini API
- Voyage embeddings
- Qdrant vector database
- SQLite + JSON persistence
- Gmail API OAuth read-only access
- Docker, pytest, ruff

## Installation

```powershell
python -m venv .personal_schedule
.\.personal_schedule\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
copy .env.example .env
```

Set the required API keys in `.env`:

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

## Run the application

```powershell
.\.personal_schedule\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open the interface:

```text
http://localhost:8000/
```

API documentation:

```text
http://localhost:8000/docs
```

## Choose an LLM

```env
LLM_PROVIDER=openai
LLM_PROVIDER=gemini
LLM_PROVIDER=compare
```

- `openai`: use GPT only.
- `gemini`: use Gemini only.
- `compare`: call both GPT and Gemini for comparison.

## Gmail

To scan Gmail:

1. Enable the Gmail API in Google Cloud Console.
2. Create an OAuth Client of type Desktop app.
3. Store the credentials file at:

```text
secrets/gmail_credentials.json
```

Add the following to `.env`:

```env
GMAIL_CREDENTIALS_PATH=secrets/gmail_credentials.json
GMAIL_TOKEN_PATH=data/gmail_token.json
GMAIL_SCAN_DAYS=3
GMAIL_MAX_RESULTS=50
```

The first time you click “Scan Gmail and update schedule”, the browser opens OAuth so you can choose a Gmail account.

## Qdrant

Schedules are stored locally in SQLite/JSON. The payload is then embedded with Voyage and upserted to Qdrant.

Recommended Qdrant collection:

```text
collection: personal_productivity_memory
vector name: dense
vector size: 1024
distance: Cosine
```

Feedback and execution logs are also embedded and stored in the same collection, with payload fields such as
`user_id`, `plan_id`, `block_id` or the feedback date. SQLite/JSON remains the source of truth;
Qdrant provides personal-memory retrieval and does not make the application lose data when the cloud service is temporarily unavailable.

## APIs for real-world tracking

- `POST /v1/schedule/{plan_id}/blocks/{block_id}/status`: update status and actual duration.
- `POST /v1/feedback/daily`: save energy, focus, effective period and procrastination reasons.
- `GET /v1/analytics/productivity?user_id=...`: view completion, delay and behavior statistics.
- `GET /v1/profile?user_id=...`: get the current personalization profile.
- `POST /v1/workflow/recalculate`: rebuild the current schedule using behavior history while preserving `plan_id`.
- `DELETE /v1/profile/{user_id}`: delete the user's schedule, feedback and execution logs.

## AI conversational assistant

Users can enter natural-language requests in Vietnamese to create, edit, delete or reschedule items. The AI retrieves personal memory, classifies the intent, updates the current schedule and stores a decision audit.

```text
POST /v1/assistant/message
GET  /v1/schedule/insights?user_id=...
GET  /v1/decisions?user_id=...
POST /v1/schedule/apply-suggestion
```

Example:

```json
{
  "user_id": "demo-user",
  "message": "From tomorrow I no longer want to study mathematics; keep the other tasks."
}
```

GPT/Gemini are compared only when `LLM_PROVIDER=compare` is configured. If the LLM or Qdrant fails,
the local schedule is still saved and the response contains a warning.

## Docker

```powershell
docker compose up --build
```

Or build the image manually:

```powershell
docker build -t personal-schedule-ai .
docker run --env-file .env -p 8000:8000 personal-schedule-ai
```

## Checks

```powershell
ruff check .
python -m pytest
python -m evals.run --dataset evals/data/golden.jsonl
```
