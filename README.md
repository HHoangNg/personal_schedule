# AI Personal Productivity OS

AI Personal Productivity OS is an AI-powered personal scheduling system. It builds a 14-day plan from tasks, deadlines and notes, with optional Gmail and Google Calendar imports.

## What does the system do?

Each user has one current plan identified by `user_id`. A submitted task form replaces the previous task list; an update containing only additional notes keeps the existing tasks. The application records execution and feedback to improve later suggestions.

## Main features

- Create a conflict-aware 14-day schedule with priorities, durations and deadlines.
- Lock schedule blocks and generate reviewable recalculation proposals without automatically moving locked blocks.
- Scan Gmail and import Google Calendar events in read-only mode.
- Use OpenAI, Gemini, comparison mode or the local `mock` provider.
- Store plans, execution records, feedback and decision audit data in SQLite/JSON.
- Optionally synchronize personal memory to Qdrant.
- Provide productivity analytics, feedback learning, guardrails, tests and evaluations.

## Personalization loop

```text
AI proposes a schedule → user records actual status → daily feedback
→ BehaviorProfile → AI reevaluates the next update
```

After at least 7 feedback days or 5 execution records, behavior data can adjust the schedule. Before that threshold, it is used for statistics and supporting evidence.

## Architecture

```text
app/
  api/            # FastAPI routes
  core/           # application configuration
  integrations/   # Gmail and read-only Google Calendar connectors
  llm/            # OpenAI, Gemini, comparison and mock providers
  memory/         # optional Voyage embeddings + Qdrant
  reliability/    # guardrails and confidence
  storage/        # SQLite + JSON repository
  web/            # Vietnamese web interface
  workflow/       # analysis, scheduling and proposal logic
tests/            # unit and integration tests
evals/            # golden-set evaluation
```

## Technologies

- FastAPI, Pydantic, SQLite and JSON persistence
- OpenAI API, Gemini API and local mock provider
- Optional Voyage embeddings and Qdrant vector database
- Gmail API OAuth and Google Calendar API OAuth (both read-only)
- Docker Compose, GitHub Actions, pytest and ruff

## Installation

```powershell
python -m venv .personal_schedule
.\.personal_schedule\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e ".[dev]"
copy .env.example .env
```

`.env` is local-only and must never be committed. Safe defaults are:

```env
LLM_PROVIDER=mock
QDRANT_SYNC_ENABLED=false
DATABASE_PATH=data/productivity.db
```

## Run the application

```powershell
.\.personal_schedule\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open the interface:

```text
http://localhost:8000/
```

API documentation and health check:

```text
http://localhost:8000/docs
http://localhost:8000/health
```

## Choose an LLM

```env
LLM_PROVIDER=mock
LLM_PROVIDER=openai
LLM_PROVIDER=gemini
LLM_PROVIDER=compare
```

- `mock`: deterministic local provider; used by CI.
- `openai`: requires `OPENAI_API_KEY`.
- `gemini`: requires `GEMINI_API_KEY`.
- `compare`: requires both keys.

## Gmail

To scan Gmail, enable the Gmail API, create a Desktop OAuth client and store its credentials at `secrets/gmail_credentials.json`. The first scan opens OAuth; Gmail access is read-only.

For Google Calendar, enable Calendar API, store a Desktop OAuth credential at `secrets/google_calendar_credentials.json`, then call `POST /v1/integrations/calendar/google/connect` once. Call `POST /v1/integrations/calendar/google/sync` to import events. The token is stored locally in `data/google_calendar_token.json`; the integration never edits Google events.

## Qdrant

SQLite/JSON is the source of truth. When `QDRANT_SYNC_ENABLED=true` and Qdrant/Voyage credentials are configured, the application also synchronizes personal-memory payloads to Qdrant. A Qdrant outage does not prevent a local schedule from being saved.

## APIs for real-world tracking

- `POST /v1/schedule/{plan_id}/blocks/{block_id}/status`: update status and actual duration.
- `POST /v1/schedule/{plan_id}/blocks/{block_id}/lock`: lock or unlock a block.
- `POST /v1/feedback/daily`: save feedback.
- `GET /v1/analytics/productivity?user_id=...`: view statistics.
- `GET /v1/profile?user_id=...`: get the personalization profile.
- `POST /v1/workflow/proposals/recalculate`: create a reviewable recalculation proposal.
- `DELETE /v1/profile/{user_id}`: delete the user's stored records.

## AI conversational assistant

Users can enter Vietnamese requests to create, edit, delete or reschedule items. The assistant updates the current schedule and stores a decision audit.

```text
POST /v1/assistant/message
GET  /v1/schedule/insights?user_id=...
GET  /v1/decisions?user_id=...
POST /v1/schedule/apply-suggestion
```

If an LLM or Qdrant is unavailable, the local schedule is still saved and the response contains a warning.

## Docker

```powershell
docker compose up --build
```

Compose keeps SQLite and OAuth token data in the `api_data` volume, uses `LLM_PROVIDER=mock` and disables Qdrant sync by default. Start optional Qdrant with:

```powershell
docker compose --profile qdrant up --build
```

The API checks `/health` and restarts with `unless-stopped`. Production secrets must come from a secret manager or a read-only mount; do not put them in the image or repository.

## Checks

```powershell
ruff check app tests evals
python -m pytest
python -m evals.run --dataset evals/data/golden.jsonl
docker compose config --quiet
```

GitHub Actions runs these checks with the mock provider. A push to `main` publishes `ghcr.io/hhoangng/personal_schedule:latest`; the image job requires `packages: write`.
