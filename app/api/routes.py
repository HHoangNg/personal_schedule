from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.settings import Settings, get_settings
from app.integrations.gmail import GmailScheduleImporter
from app.schemas import GmailScanRequest, PlanSummary, TaskInput, WorkflowRequest, WorkflowResult
from app.storage.repository import PlanRepository
from app.workflow.calendar_workflow import CalendarWorkflow

router = APIRouter(prefix="/v1")
MAX_NOTE_CHARS = 950


def repository(settings: Settings) -> PlanRepository:
    return PlanRepository(
        settings.database_path,
        qdrant_url=settings.qdrant_url,
        qdrant_api_key=settings.qdrant_api_key,
        qdrant_collection=settings.qdrant_collection,
        qdrant_vector_size=settings.qdrant_vector_size,
        qdrant_vector_name=settings.qdrant_vector_name,
        qdrant_sync_enabled=settings.qdrant_sync_enabled,
        voyage_api_key=settings.voyage_api_key,
        voyage_model=settings.voyage_model,
    )


def current_plan(repo: PlanRepository, user_id: str) -> WorkflowResult | None:
    plans = repo.list_for_user(user_id)
    return repo.get_for_user(user_id, plans[0].plan_id) if plans else None


def merge_with_existing_request(
    request: WorkflowRequest, existing: WorkflowResult | None
) -> WorkflowRequest:
    previous = existing.plan_input if existing and existing.plan_input else None
    if not previous:
        return request

    previous_tasks = {task.title.casefold(): task for task in previous.task_inputs if task.title.strip()}
    merged_tasks: list[TaskInput] = list(previous.task_inputs)
    for task in request.task_inputs:
        key = task.title.casefold().strip()
        if key and key in previous_tasks:
            merged_tasks = [
                task if item.title.casefold().strip() == key else item for item in merged_tasks
            ]
        else:
            merged_tasks.append(task)

    notes = _merge_notes(previous.planning_notes, request.planning_notes)

    raw_parts = [part for part in [previous.raw_input.strip(), request.raw_input.strip()] if part]
    return previous.model_copy(
        update={
            "display_name": request.display_name.strip() or previous.display_name,
            "raw_input": "\n".join(dict.fromkeys(raw_parts)),
            "task_inputs": merged_tasks,
            "planning_notes": notes,
            "horizon_days": request.horizon_days,
        }
    )


def _merge_notes(previous: str, new: str) -> str:
    parts: list[str] = []
    for value in [previous.strip(), new.strip()]:
        if not value:
            continue
        cleaned = "\n".join(
            line
            for line in value.splitlines()
            if line.strip() not in {"Yêu cầu riêng trước đó:", "Cập nhật mới:"}
        ).strip()
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    merged = "\n\n".join(parts)
    return merged[-MAX_NOTE_CHARS:]


@router.post("/workflow/plan", response_model=WorkflowResult)
def create_plan(request: WorkflowRequest, settings: Settings = Depends(get_settings)):
    repo = repository(settings)
    merged_request = merge_with_existing_request(request, current_plan(repo, request.user_id))
    plan = CalendarWorkflow(settings).run(merged_request)
    return repo.save(request.user_id, plan)


@router.post("/integrations/gmail/scan", response_model=WorkflowResult)
def scan_gmail(request: GmailScanRequest, settings: Settings = Depends(get_settings)):
    repo = repository(settings)
    workflow_request = GmailScheduleImporter(settings).build_request(
        request.user_id,
        request.display_name,
        request.days,
        request.max_results,
    )
    merged_request = merge_with_existing_request(workflow_request, current_plan(repo, request.user_id))
    plan = CalendarWorkflow(settings).run(merged_request)
    return repo.save(request.user_id, plan)


@router.get("/plans", response_model=list[PlanSummary])
def list_plans(
    user_id: str = Query(min_length=1), settings: Settings = Depends(get_settings)
):
    return PlanRepository(settings.database_path).list_for_user(user_id)


@router.get("/plans/{plan_id}", response_model=WorkflowResult)
def get_plan(plan_id: str, user_id: str = Query(min_length=1), settings: Settings = Depends(get_settings)):
    plan = PlanRepository(settings.database_path).get_for_user(user_id, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Không tìm thấy kế hoạch đã lưu.")
    return plan
