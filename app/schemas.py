from datetime import date, datetime, time, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Task(BaseModel):
    title: str = Field(min_length=1)
    deadline_mode: Literal["daily", "specific", "none"] = "none"
    deadline: date | None = None
    deadline_time: time | None = None
    deadline_source: str | None = None
    type: str = "general"
    priority: Literal["low", "medium", "high"] = "medium"
    estimated_minutes: int = Field(default=30, ge=5, le=480)
    occurrences_per_day: int = Field(default=1, ge=1, le=6)


class Subtask(BaseModel):
    title: str
    estimated_minutes: int = Field(ge=5, le=480)
    definition_of_done: str


class ScheduleSession(BaseModel):
    block_id: str = Field(default_factory=lambda: str(uuid4()))
    date: date
    start_time: str
    end_time: str
    task_title: str
    minutes: int = Field(ge=5, le=480)
    block_type: Literal["task", "personal", "rest", "work"] = "task"
    period: Literal["morning", "afternoon", "evening", "night"] = "evening"
    status: Literal["planned", "completed", "skipped", "rescheduled", "partial"] = "planned"
    actual_minutes: int | None = Field(default=None, ge=0, le=1440)
    status_note: str = ""
    source: Literal["ai", "user", "external"] = "ai"
    external_event_id: str | None = Field(default=None, max_length=512)
    is_locked: bool = False
    lock_reason: str = Field(default="", max_length=500)
    explanation: "ScheduleExplanation" = Field(default_factory=lambda: ScheduleExplanation())


class ScheduleExplanation(BaseModel):
    why_this_slot: str = "Chưa có giải thích chi tiết cho khối lịch này."
    evidence: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(default=0.0, ge=0, le=1)
    alternatives: list[str] = Field(default_factory=list, max_length=3)
    policy: str = "proposal-first"


class GoalAnalysis(BaseModel):
    summary: str = ""
    success_criteria: list[str] = []
    assumptions: list[str] = []


class Risk(BaseModel):
    title: str
    severity: Literal["low", "medium", "high"] = "medium"
    mitigation: str


class FocusSession(BaseModel):
    task_title: str
    objective: str
    method: str
    checklist: list[str] = []


class ReviewQuestion(BaseModel):
    question: str
    purpose: str


class BehaviorAdvice(BaseModel):
    task_title: str
    recommended_minutes: int | None = Field(default=None, ge=5, le=480)
    preferred_period: Literal["morning", "afternoon", "evening", "night", "flexible"] = "flexible"
    split_into_sessions: bool = False
    confidence: float = Field(default=0.0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class TaskInput(BaseModel):
    title: str = ""
    deadline_mode: Literal["daily", "specific", "none"] = "none"
    deadline_at: datetime | None = None
    type: str = "general"
    priority: Literal["low", "medium", "high"] = "medium"
    estimated_minutes: int = Field(default=30, ge=5, le=480)

    @model_validator(mode="after")
    def validate_deadline(self):
        if self.deadline_mode == "specific" and self.deadline_at is None:
            raise ValueError("Deadline cụ thể phải có ngày và giờ.")
        return self


class ScheduleExclusion(BaseModel):
    task_title: str = Field(min_length=1)
    from_date: date | None = None
    reason: str = Field(default="", max_length=500)


class WorkflowRequest(BaseModel):
    user_id: str = Field(min_length=1)
    display_name: str = ""
    raw_input: str = ""
    task_inputs: list[TaskInput] = Field(default_factory=list)
    schedule_exclusions: list[ScheduleExclusion] = Field(default_factory=list)
    available_minutes_per_day: int = Field(default=120, ge=15, le=1440)
    deadline_at: datetime | None = None
    # Broad safe bounds; the scheduler chooses actual slots from user evidence.
    daily_start_time: time = time(hour=5)
    daily_end_time: time = time(hour=23)
    preferred_weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1)
    energy_peak: Literal["morning", "afternoon", "evening", "flexible"] = "flexible"
    work_style: Literal["deep_focus", "short_sprints", "flexible"] = "flexible"
    focus_minutes: int = Field(default=25, ge=15, le=120)
    existing_commitments: str = Field(default="", max_length=1000)
    planning_notes: str = Field(default="", max_length=1000)
    horizon_days: int = Field(default=14, ge=14, le=14)
    memory_context: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="before")
    @classmethod
    def allow_planning_notes_only(cls, data):
        if not isinstance(data, dict):
            return data
        has_tasks = bool(data.get("task_inputs"))
        raw_input = str(data.get("raw_input") or "").strip()
        planning_notes = str(data.get("planning_notes") or "").strip()
        if not has_tasks and len(raw_input) < 3 and len(planning_notes) >= 3:
            data = {**data, "raw_input": planning_notes}
        return data

    @model_validator(mode="after")
    def validate_input(self):
        if not self.task_inputs and len(self.raw_input.strip()) < 3:
            raise ValueError("Cần thêm ít nhất một công việc hoặc mô tả mục tiêu.")
        return self


class WorkflowResult(BaseModel):
    plan_id: str | None = None
    created_at: datetime | None = None
    plan_input: WorkflowRequest | None = None
    llm_provider: str
    llm_model: str
    llm_raw_preview: str = ""
    tasks: list[Task]
    priorities: list[dict]
    subtasks: dict[str, list[Subtask]]
    schedule: list[ScheduleSession]
    goal_analysis: GoalAnalysis = Field(default_factory=GoalAnalysis)
    schedule_reasoning: str = ""
    risks: list[Risk] = []
    focus_sessions: list[FocusSession] = []
    review_questions: list[ReviewQuestion] = []
    adaptation_suggestions: list[BehaviorAdvice] = []
    warnings: list[str] = []
    needs_confirmation: bool = False
    confidence: float = Field(ge=0, le=1)


class ExecutionLog(BaseModel):
    log_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    task_title: str
    scheduled_date: date
    scheduled_start_time: str
    scheduled_end_time: str
    status: Literal["planned", "completed", "skipped", "rescheduled", "partial"]
    actual_minutes: int | None = Field(default=None, ge=0, le=1440)
    reason: str = Field(default="", max_length=1000)
    recorded_at: datetime = Field(default_factory=utc_now)


class ExecutionStatusRequest(BaseModel):
    status: Literal["completed", "skipped", "rescheduled", "partial"]
    actual_minutes: int | None = Field(default=None, ge=0, le=1440)
    reason: str = Field(default="", max_length=1000)


class DailyFeedback(BaseModel):
    feedback_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = Field(min_length=1)
    feedback_date: date
    energy: int = Field(ge=1, le=5)
    focus: int = Field(ge=1, le=5)
    effective_period: Literal["morning", "afternoon", "evening", "night", "flexible"] = "flexible"
    schedule_feeling: Literal["too_light", "balanced", "too_heavy", "unrealistic"] = "balanced"
    procrastinated_tasks: list[str] = Field(default_factory=list, max_length=30)
    note: str = Field(default="", max_length=2000)
    created_at: datetime = Field(default_factory=utc_now)


class BehaviorProfile(BaseModel):
    user_id: str
    sample_days: int = 0
    execution_count: int = 0
    completion_rate: float = 0.0
    procrastination_rate: float = 0.0
    average_duration_ratio: float | None = None
    effective_period: str = "chưa đủ dữ liệu"
    effective_period_scores: dict[str, float] = Field(default_factory=dict)
    commonly_skipped_tasks: list[str] = Field(default_factory=list)
    priority_adjustments: dict[str, int] = Field(default_factory=dict)
    priority_evidence: dict[str, list[str]] = Field(default_factory=dict)
    enough_data_for_auto_apply: bool = False
    generated_at: datetime = Field(default_factory=utc_now)


class ProductivityAnalytics(BaseModel):
    user_id: str
    profile: BehaviorProfile
    metrics: dict[str, float | int | None] = Field(default_factory=dict)


class LockBlockRequest(BaseModel):
    locked: bool
    reason: str = Field(default="", max_length=500)


class ScheduleProposalRequest(BaseModel):
    user_id: str = Field(min_length=1)
    trigger: Literal["manual", "new_task", "deadline_near", "progress_delay", "calendar_change"] = "manual"


class ScheduleProposal(BaseModel):
    proposal_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    trigger: str
    risk: Literal["low", "medium", "high"] = "medium"
    requires_confirmation: bool = True
    applied: bool = False
    changes: list[dict] = Field(default_factory=list)
    reasoning: str = Field(default="", max_length=2000)
    created_at: datetime = Field(default_factory=utc_now)


class CalendarSyncRequest(BaseModel):
    user_id: str = Field(min_length=1)
    days: int = Field(default=14, ge=1, le=31)
    max_results: int = Field(default=250, ge=1, le=2500)


class CalendarEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=512)
    summary: str = Field(default="", max_length=500)
    start_at: datetime
    end_at: datetime
    updated_at: datetime | None = None
    attendee_count: int = Field(default=0, ge=0)
    is_cancelled: bool = False
    etag: str = Field(default="", max_length=512)


class CalendarSyncResult(BaseModel):
    provider: Literal["google"] = "google"
    user_id: str
    event_count: int = 0
    synced_at: datetime = Field(default_factory=utc_now)
    warnings: list[str] = Field(default_factory=list)


class RecalculateRequest(BaseModel):
    user_id: str = Field(min_length=1)
    confirm_advice: bool = False


class AssistantMessageRequest(BaseModel):
    user_id: str = Field(min_length=1)
    message: str = Field(min_length=3, max_length=4000)
    display_name: str = ""


class AssistantIntent(BaseModel):
    action: Literal[
        "create_task",
        "update_task",
        "delete_task",
        "reschedule",
        "add_commitment",
        "feedback",
        "unknown",
    ] = "unknown"
    task_titles: list[str] = Field(default_factory=list, max_length=20)
    planner_message: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)
    requires_confirmation: bool = False
    reasoning: str = ""


class DecisionAudit(BaseModel):
    decision_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str = Field(min_length=1)
    plan_id: str | None = None
    action: str
    message: str = Field(max_length=4000)
    provider: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)
    requires_confirmation: bool = False
    reasoning: str = ""
    policy: str = ""
    risk: Literal["low", "medium", "high"] = "medium"
    change_summary: str = Field(default="", max_length=2000)
    applied: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class AssistantMessageResponse(BaseModel):
    intent: AssistantIntent
    plan: WorkflowResult | None = None
    insights: ProductivityAnalytics | None = None
    decision: DecisionAudit
    warnings: list[str] = Field(default_factory=list)


class SuggestionApplyRequest(BaseModel):
    user_id: str = Field(min_length=1)
    task_title: str = Field(min_length=1)
    recommended_minutes: int | None = Field(default=None, ge=5, le=480)
    preferred_period: Literal["morning", "afternoon", "evening", "night", "flexible"] = "flexible"
    confirm: bool = False


class PlanSummary(BaseModel):
    plan_id: str
    created_at: datetime
    title: str
    task_count: int


class GmailScanRequest(BaseModel):
    user_id: str = Field(min_length=1)
    display_name: str = ""
    days: int = Field(default=3, ge=1, le=30)
    max_results: int = Field(default=50, ge=1, le=200)
