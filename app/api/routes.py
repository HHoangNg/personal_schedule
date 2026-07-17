import json
import re
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.settings import Settings, get_settings
from app.integrations.gmail import GmailScheduleImporter
from app.schemas import (
    AssistantIntent,
    AssistantMessageRequest,
    AssistantMessageResponse,
    DailyFeedback,
    DecisionAudit,
    ExecutionStatusRequest,
    GmailScanRequest,
    PlanSummary,
    ProductivityAnalytics,
    RecalculateRequest,
    ScheduleExclusion,
    SuggestionApplyRequest,
    TaskInput,
    WorkflowRequest,
    WorkflowResult,
)
from app.storage.repository import PlanRepository
from app.workflow.calendar_workflow import CalendarWorkflow

router = APIRouter(prefix="/v1")
MAX_NOTE_CHARS = 950
DELETE_INTENT_RE = re.compile(
    r"\b("
    r"không\s+muốn|khong\s+muon|không\s+cần|khong\s+can|"
    r"bỏ|bo|xóa|xoá|xoa|hủy|huỷ|huy|"
    r"dừng|dung|ngừng|ngung|loại\s+bỏ|loai\s+bo"
    r")\b",
    re.IGNORECASE,
)
WEEKDAY_ALIASES = {
    "2": 0, "hai": 0,
    "3": 1, "ba": 1,
    "4": 2, "tư": 2, "tu": 2,
    "5": 3, "năm": 3, "nam": 3,
    "6": 4, "sáu": 4, "sau": 4,
    "7": 5, "bảy": 5, "bay": 5,
    "chủ nhật": 6, "chu nhat": 6, "cn": 6,
}

ASSISTANT_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [
            "create_task", "update_task", "delete_task", "reschedule",
            "add_commitment", "feedback", "unknown",
        ]},
        "task_titles": {"type": "array", "items": {"type": "string"}},
        "planner_message": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "requires_confirmation": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": [
        "action", "task_titles", "planner_message", "confidence",
        "requires_confirmation", "reasoning",
    ],
}


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

    exclusions = _merge_exclusions(
        previous.schedule_exclusions,
        _detect_delete_intents(request, existing),
    )
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
    merged_tasks = _apply_full_task_deletions(merged_tasks, exclusions)

    cleaned_previous_notes = _remove_fully_deleted_titles(previous.planning_notes, exclusions)
    cleaned_previous_raw = _remove_fully_deleted_titles(previous.raw_input, exclusions)
    cleaned_new_notes = _remove_delete_command_lines(request.planning_notes)
    cleaned_new_raw = _remove_delete_command_lines(request.raw_input)
    notes = _merge_notes(cleaned_previous_notes, cleaned_new_notes)

    raw_parts = [part for part in [cleaned_previous_raw.strip(), cleaned_new_raw.strip()] if part]
    return previous.model_copy(
        update={
            "display_name": request.display_name.strip() or previous.display_name,
            "raw_input": "\n".join(dict.fromkeys(raw_parts)),
            "task_inputs": merged_tasks,
            "schedule_exclusions": exclusions,
            "planning_notes": notes,
            "horizon_days": request.horizon_days,
            "memory_context": request.memory_context,
        }
    )


def _heuristic_assistant_intent(message: str) -> AssistantIntent:
    normalized = message.casefold()
    if DELETE_INTENT_RE.search(normalized):
        action = "delete_task"
    elif re.search(r"dời|doi|chuyển|chuyen|đổi giờ|doi gio|sắp lại|sap lai", normalized):
        action = "reschedule"
    elif re.search(r"đi làm|di lam|bận|ban|họp|hop|cam kết|cam ket", normalized):
        action = "add_commitment"
    elif re.search(r"feedback|năng lượng|nang luong|tập trung|tap trung|hôm nay|hom nay", normalized):
        action = "feedback"
    elif re.search(r"sửa|sua|cập nhật|cap nhat|thêm|them|học|hoc|làm|lam", normalized):
        action = "update_task"
    else:
        action = "create_task"
    return AssistantIntent(
        action=action,
        planner_message=message,
        confidence=0.45,
        requires_confirmation=action in {"delete_task", "reschedule"},
        reasoning="Suy luận cục bộ do LLM chưa trả về intent có cấu trúc.",
    )


def _extract_assistant_intent(settings: Settings, request: AssistantMessageRequest, existing: WorkflowResult | None):
    context = {
        "message": request.message,
        "current_tasks": [task.title for task in (existing.tasks if existing else [])],
        "current_schedule": [
            {"date": block.date.isoformat(), "time": f"{block.start_time}-{block.end_time}", "title": block.task_title}
            for block in (existing.schedule if existing else [])
            if block.block_type in {"task", "work"}
        ][:40],
        "instruction": (
            "Phân loại đúng một action. Giữ planner_message là yêu cầu đầy đủ bằng tiếng Việt để bộ lập lịch xử lý. "
            "Không tự tạo deadline hoặc thời gian nếu người dùng không nói. Nếu xóa/dời việc quan trọng, requires_confirmation=true."
        ),
    }
    try:
        provider = CalendarWorkflow(settings).provider
        response = provider.generate_json(json.dumps(context, ensure_ascii=False), ASSISTANT_INTENT_SCHEMA)
        parsed = AssistantIntent.model_validate(response.data)
        if parsed.action == "unknown":
            fallback = _heuristic_assistant_intent(request.message)
            return fallback, response.provider, [
                "LLM trả về intent unknown; đã dùng fallback an toàn để không bỏ qua yêu cầu rõ ràng của người dùng."
            ]
        return parsed, response.provider, []
    except Exception as exc:
        return _heuristic_assistant_intent(request.message), "local-intent-fallback", [
            f"Không phân tích được intent bằng LLM: {type(exc).__name__}: {exc}"
        ]


def _detect_delete_intents(
    request: WorkflowRequest, existing: WorkflowResult
) -> list[ScheduleExclusion]:
    text = "\n".join(
        part.strip() for part in [request.raw_input, request.planning_notes] if part.strip()
    )
    if not text or not DELETE_INTENT_RE.search(text):
        return []
    candidates = _existing_task_titles(existing)
    matched_titles = [
        title for title in candidates if re.search(re.escape(title), text, flags=re.IGNORECASE)
    ]
    if not matched_titles and len(request.task_inputs) == 1 and request.task_inputs[0].title.strip():
        matched_titles = [request.task_inputs[0].title.strip()]
    if not matched_titles:
        return []
    from_date = _extract_from_date(text)
    return [
        ScheduleExclusion(task_title=title, from_date=from_date, reason=text[:500])
        for title in matched_titles
    ]


def _existing_task_titles(existing: WorkflowResult) -> list[str]:
    titles: list[str] = []
    if existing.plan_input:
        titles.extend(task.title.strip() for task in existing.plan_input.task_inputs if task.title.strip())
    titles.extend(task.title.strip() for task in existing.tasks if task.title.strip())
    titles.extend(
        block.task_title.strip()
        for block in existing.schedule
        if block.block_type in {"task", "work"} and block.task_title.strip()
    )
    return list(dict.fromkeys(titles))


def _merge_exclusions(
    previous: list[ScheduleExclusion], new: list[ScheduleExclusion]
) -> list[ScheduleExclusion]:
    merged: dict[tuple[str, str], ScheduleExclusion] = {}
    for item in [*previous, *new]:
        key = (item.task_title.casefold().strip(), item.from_date.isoformat() if item.from_date else "")
        merged[key] = item
    return list(merged.values())


def _apply_full_task_deletions(
    tasks: list[TaskInput], exclusions: list[ScheduleExclusion]
) -> list[TaskInput]:
    deleted = {item.task_title.casefold().strip() for item in exclusions if item.from_date is None}
    if not deleted:
        return tasks
    return [task for task in tasks if task.title.casefold().strip() not in deleted]


def _remove_fully_deleted_titles(text: str, exclusions: list[ScheduleExclusion]) -> str:
    result = text
    for exclusion in exclusions:
        if exclusion.from_date is not None:
            continue
        result = re.sub(re.escape(exclusion.task_title), "", result, flags=re.IGNORECASE)
    result = re.sub(r"\s+(và|va)\s*(,|\n|$)", r"\2", result, flags=re.IGNORECASE)
    result = re.sub(r"(^|\n|,)\s*(và|va)\s+", r"\1", result, flags=re.IGNORECASE)
    result = re.sub(r"[,\s]+$", "", result)
    return result.strip()


def _remove_delete_command_lines(text: str) -> str:
    lines = [line for line in text.splitlines() if not DELETE_INTENT_RE.search(line)]
    return "\n".join(line.strip() for line in lines if line.strip())


def _extract_from_date(text: str) -> date | None:
    normalized = text.casefold()
    if re.search(r"\btừ\s+hôm\s+nay\b|\btu\s+hom\s+nay\b", normalized):
        return date.today()
    if re.search(r"\btừ\s+mai\b|\btu\s+mai\b|\bngày\s+mai\b|\bngay\s+mai\b", normalized):
        return date.today() + timedelta(days=1)

    iso_match = re.search(r"(?:từ|tu|sau|kể từ|ke tu)\s*(?:ngày|ngay)?\s*(\d{4}-\d{2}-\d{2})", normalized)
    if iso_match:
        try:
            return datetime.strptime(iso_match.group(1), "%Y-%m-%d").date()
        except ValueError:
            return None

    slash_match = re.search(
        r"(?:từ|tu|sau|kể từ|ke tu)\s*(?:ngày|ngay)?\s*(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?",
        normalized,
    )
    if slash_match:
        day = int(slash_match.group(1))
        month = int(slash_match.group(2))
        year = int(slash_match.group(3) or date.today().year)
        try:
            parsed = date(year, month, day)
        except ValueError:
            return None
        if slash_match.group(3) is None and parsed < date.today():
            parsed = date(year + 1, month, day)
        return parsed

    weekday_match = re.search(r"(?:từ|tu)\s+thứ\s*(2|3|4|5|6|7|hai|ba|tư|tu|năm|nam|sáu|sau|bảy|bay)", normalized)
    if weekday_match:
        return _next_weekday(WEEKDAY_ALIASES[weekday_match.group(1)])
    sunday_match = re.search(r"(?:từ|tu)\s+(chủ nhật|chu nhat|cn)", normalized)
    if sunday_match:
        return _next_weekday(6)
    return None


def _next_weekday(target: int) -> date:
    today = date.today()
    delta = (target - today.weekday()) % 7
    return today + timedelta(days=delta)


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
    request = request.model_copy(update={"memory_context": repo.search_memory(request.user_id, request.raw_input or request.planning_notes)})
    merged_request = merge_with_existing_request(request, current_plan(repo, request.user_id))
    plan = CalendarWorkflow(settings).run(
        merged_request, repo.behavior_profile(request.user_id).model_dump(mode="json")
    )
    return repo.save(request.user_id, plan)


@router.post("/assistant/message", response_model=AssistantMessageResponse)
def assistant_message(
    request: AssistantMessageRequest, settings: Settings = Depends(get_settings)
):
    repo = repository(settings)
    existing = current_plan(repo, request.user_id)
    intent, provider, warnings = _extract_assistant_intent(settings, request, existing)
    memory = repo.search_memory(request.user_id, request.message)
    if intent.action == "feedback":
        decision = repo.record_decision(DecisionAudit(
            user_id=request.user_id, action=intent.action, message=request.message,
            provider=provider, confidence=intent.confidence,
            requires_confirmation=False, reasoning=intent.reasoning, applied=False,
        ))
        return AssistantMessageResponse(
            intent=intent, insights=repo.analytics(request.user_id), decision=decision,
            warnings=[*warnings, "Hãy dùng form feedback cuối ngày để lưu điểm năng lượng và tập trung."],
        )

    planner_message = intent.planner_message.strip() or request.message
    workflow_request = WorkflowRequest(
        user_id=request.user_id,
        display_name=request.display_name,
        raw_input=planner_message,
        planning_notes=planner_message,
        memory_context=memory,
    )
    merged_request = merge_with_existing_request(workflow_request, existing)
    if not merged_request.raw_input.strip() and not merged_request.task_inputs:
        # WorkflowRequest requires an auditable input even when the command removes the last task.
        merged_request = merged_request.model_copy(update={"raw_input": planner_message})
    plan = CalendarWorkflow(settings).run(
        merged_request, repo.behavior_profile(request.user_id).model_dump(mode="json")
    )
    saved = repo.save(request.user_id, plan)
    warnings.extend(saved.warnings)
    decision = repo.record_decision(DecisionAudit(
        user_id=request.user_id, plan_id=saved.plan_id, action=intent.action,
        message=request.message, provider=provider, confidence=intent.confidence,
        requires_confirmation=intent.requires_confirmation,
        reasoning=intent.reasoning, applied=True,
    ))
    return AssistantMessageResponse(intent=intent, plan=saved, decision=decision, warnings=warnings)


@router.post("/integrations/gmail/scan", response_model=WorkflowResult)
def scan_gmail(request: GmailScanRequest, settings: Settings = Depends(get_settings)):
    repo = repository(settings)
    workflow_request = GmailScheduleImporter(settings).build_request(
        request.user_id,
        request.display_name,
        request.days,
        request.max_results,
    )
    workflow_request = workflow_request.model_copy(
        update={"memory_context": repo.search_memory(request.user_id, "Gmail lịch trình")}
    )
    merged_request = merge_with_existing_request(workflow_request, current_plan(repo, request.user_id))
    plan = CalendarWorkflow(settings).run(
        merged_request, repo.behavior_profile(request.user_id).model_dump(mode="json")
    )
    return repo.save(request.user_id, plan)


@router.post("/schedule/{plan_id}/blocks/{block_id}/status", response_model=WorkflowResult)
def update_block_status(
    plan_id: str,
    block_id: str,
    request: ExecutionStatusRequest,
    user_id: str = Query(min_length=1),
    settings: Settings = Depends(get_settings),
):
    repo = repository(settings)
    try:
        plan, _ = repo.update_block_status(
            user_id, plan_id, block_id, request.status, request.actual_minutes, request.reason
        )
        return plan
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/feedback/daily", response_model=DailyFeedback)
def save_daily_feedback(feedback: DailyFeedback, settings: Settings = Depends(get_settings)):
    return repository(settings).save_daily_feedback(feedback)


@router.get("/analytics/productivity", response_model=ProductivityAnalytics)
def productivity_analytics(
    user_id: str = Query(min_length=1), settings: Settings = Depends(get_settings)
):
    return repository(settings).analytics(user_id)


@router.get("/schedule/insights", response_model=ProductivityAnalytics)
def schedule_insights(
    user_id: str = Query(min_length=1), settings: Settings = Depends(get_settings)
):
    return repository(settings).analytics(user_id)


@router.get("/decisions", response_model=list[DecisionAudit])
def decisions(
    user_id: str = Query(min_length=1), limit: int = Query(default=50, ge=1, le=200),
    settings: Settings = Depends(get_settings),
):
    return repository(settings).list_decisions(user_id, limit)


@router.post("/schedule/apply-suggestion", response_model=WorkflowResult)
def apply_suggestion(
    request: SuggestionApplyRequest, settings: Settings = Depends(get_settings)
):
    if not request.confirm:
        raise HTTPException(status_code=400, detail="Cần xác nhận trước khi áp dụng đề xuất.")
    repo = repository(settings)
    existing = current_plan(repo, request.user_id)
    if not existing or not existing.plan_input:
        raise HTTPException(status_code=404, detail="Người dùng chưa có lịch để áp dụng đề xuất.")
    task_inputs = list(existing.plan_input.task_inputs)
    matched = False
    for index, item in enumerate(task_inputs):
        if item.title.casefold().strip() == request.task_title.casefold().strip():
            if request.recommended_minutes is not None:
                task_inputs[index] = item.model_copy(update={"estimated_minutes": request.recommended_minutes})
            matched = True
            break
    if not matched:
        raise HTTPException(status_code=404, detail="Không tìm thấy công việc trong lịch hiện tại.")
    notes = existing.plan_input.planning_notes
    if request.preferred_period != "flexible":
        notes = f"{notes}\nĐặt {request.task_title} vào buổi {request.preferred_period}.".strip()
    updated_request = existing.plan_input.model_copy(update={"task_inputs": task_inputs, "planning_notes": notes})
    plan = CalendarWorkflow(settings).run(
        updated_request, repo.behavior_profile(request.user_id).model_dump(mode="json")
    )
    saved = repo.save(request.user_id, plan)
    repo.record_decision(DecisionAudit(
        user_id=request.user_id, plan_id=saved.plan_id, action="apply_suggestion",
        message=request.task_title, provider="user-confirmed", confidence=1.0,
        reasoning="Người dùng đã xác nhận đề xuất cá nhân hóa.", applied=True,
    ))
    return saved


@router.get("/profile", response_model=dict)
def behavior_profile(user_id: str = Query(min_length=1), settings: Settings = Depends(get_settings)):
    return repository(settings).behavior_profile(user_id).model_dump(mode="json")


@router.post("/workflow/recalculate", response_model=WorkflowResult)
def recalculate(request: RecalculateRequest, settings: Settings = Depends(get_settings)):
    repo = repository(settings)
    existing = current_plan(repo, request.user_id)
    if not existing or not existing.plan_input:
        raise HTTPException(status_code=404, detail="Người dùng chưa có lịch để đánh giá lại.")
    profile = repo.behavior_profile(request.user_id).model_dump(mode="json")
    plan = CalendarWorkflow(settings).run(existing.plan_input, profile)
    return repo.save(request.user_id, plan)


@router.delete("/profile/{user_id}")
def delete_user_data(user_id: str, settings: Settings = Depends(get_settings)):
    return {"deleted_plans": repository(settings).delete_user_data(user_id), "user_id": user_id}


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
