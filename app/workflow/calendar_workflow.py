import copy
import json
import re
from datetime import date, timedelta

from app.llm.providers import StructuredLLMResponse, build_provider
from app.reliability.guardrails import confidence, validate_grounding
from app.schemas import (
    FocusSession,
    GoalAnalysis,
    ReviewQuestion,
    Risk,
    ScheduleSession,
    Subtask,
    Task,
    TaskInput,
    WorkflowRequest,
    WorkflowResult,
)
from app.workflow.personalized_service import PLAN_SCHEMA


CALENDAR_SCHEMA = copy.deepcopy(PLAN_SCHEMA)
CALENDAR_SCHEMA["properties"]["tasks"]["items"]["properties"]["occurrences_per_day"] = {
    "type": "integer", "minimum": 1, "maximum": 6,
}
CALENDAR_SCHEMA["properties"]["scheduling_intents"] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "task_title": {"type": "string"},
            "preferred_start_time": {"type": "string", "nullable": True},
            "preferred_period": {
                "type": "string",
                "enum": ["morning", "afternoon", "evening", "night", "flexible"],
            },
            "recurrence": {
                "type": "string",
                "enum": ["once", "daily", "weekdays", "alternate_days", "specific_weekday"],
            },
            "target_weekday": {"type": "integer", "nullable": True, "minimum": 0, "maximum": 6},
            "occurrences_per_day": {"type": "integer", "minimum": 1, "maximum": 6},
            "reason": {"type": "string"},
        },
        "required": [
            "task_title",
            "preferred_start_time",
            "preferred_period",
            "recurrence",
            "target_weekday",
            "occurrences_per_day",
            "reason",
        ],
    },
}

DAY_START = 6 * 60
DAY_END = 22 * 60
PERIODS = {
    "morning": (6 * 60, 12 * 60),
    "afternoon": (12 * 60, 18 * 60),
    "evening": (18 * 60, 22 * 60),
}


class CalendarWorkflow:
    """Builds one conflict-aware, 14-day calendar per user."""

    def __init__(self, settings):
        self.settings = settings
        self.provider = build_provider(settings)

    def run(self, request: WorkflowRequest) -> WorkflowResult:
        response, llm_warnings = self._generate_calendar_analysis(request)
        data = response.data
        raw_tasks = [self._validate_task(item) for item in response.data.get("tasks", [])]
        tasks = self._apply_inputs(
            raw_tasks,
            request.task_inputs,
            f"{request.raw_input}\n{request.planning_notes}",
        )
        grounding_text = request.raw_input + " " + " ".join(
            item.deadline_at.date().isoformat() for item in request.task_inputs if item.deadline_at
        )
        warnings = llm_warnings + validate_grounding(tasks, grounding_text)
        if not tasks:
            warnings.append("Chưa có công việc để xếp lịch.")
        if response.provider == "mock":
            warnings.append("Đang dùng LLM_PROVIDER=mock; ưu tiên và loại công việc chỉ là dữ liệu mô phỏng.")
        schedule, schedule_warnings = self._build_calendar(
            tasks,
            request,
            self._scheduling_intents(data.get("scheduling_intents", []), tasks),
        )
        warnings.extend(schedule_warnings)
        return WorkflowResult(
            llm_provider=response.provider,
            llm_model=self._model_label(response.provider),
            llm_raw_preview=response.raw_text[:500],
            tasks=tasks,
            priorities=[{"task": task.title, "score": self._score(task), "reason": self._reason(task)} for task in tasks],
            subtasks=self._subtasks(tasks, data.get("suggested_subtasks", [])),
            schedule=schedule,
            goal_analysis=self._goal_analysis(data.get("goal_analysis", {})),
            schedule_reasoning="Lịch 14 ngày được lấp theo ưu tiên AI, thời lượng thực tế và thông tin bổ sung.",
            risks=self._risks(data.get("risks", []), warnings),
            focus_sessions=self._focus_sessions(data.get("focus_sessions", []), warnings),
            review_questions=self._review_questions(data.get("review_questions", []), warnings),
            plan_input=request,
            warnings=warnings,
            needs_confirmation=bool(warnings),
            confidence=confidence(warnings, len(tasks)),
        )

    def _generate_calendar_analysis(self, request: WorkflowRequest) -> tuple[StructuredLLMResponse, list[str]]:
        try:
            return self.provider.generate_json(self._context(request), CALENDAR_SCHEMA), []
        except Exception as exc:
            response = self._fallback_response(request, exc)
            return response, [
                "LLM đang tạm thời không phản hồi nên hệ thống đã cập nhật lịch bằng bộ xếp lịch cục bộ.",
                f"Lỗi LLM: {type(exc).__name__}: {exc}",
            ]

    def _model_label(self, provider: str) -> str:
        if provider == "openai":
            return self.settings.openai_model
        if provider == "gemini":
            return self.settings.gemini_model
        if provider == "openai+gemini":
            return f"{self.settings.openai_model} + {self.settings.gemini_model}"
        return self.settings.llm_model

    @staticmethod
    def _goal_analysis(value: dict) -> GoalAnalysis:
        if not isinstance(value, dict):
            return GoalAnalysis()
        return GoalAnalysis(
            summary=str(value.get("summary") or value.get("analysis") or ""),
            success_criteria=[
                str(item)
                for item in value.get("success_criteria", value.get("criteria", []))
                if str(item).strip()
            ],
            assumptions=[
                str(item)
                for item in value.get("assumptions", value.get("notes", []))
                if str(item).strip()
            ],
        )

    @staticmethod
    def _risks(items: list[dict], warnings: list[str]) -> list[Risk]:
        risks: list[Risk] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            normalized = {
                "title": item.get("title")
                or item.get("risk")
                or item.get("name")
                or "Rủi ro chưa đặt tên",
                "severity": item.get("severity")
                if item.get("severity") in {"low", "medium", "high"}
                else "medium",
                "mitigation": item.get("mitigation")
                or item.get("solution")
                or item.get("suggestion")
                or item.get("action")
                or "Cần theo dõi và điều chỉnh lịch khi cần.",
            }
            try:
                risks.append(Risk.model_validate(normalized))
            except Exception as exc:
                warnings.append(f"Bỏ qua một risk LLM không hợp lệ: {type(exc).__name__}: {exc}")
        return risks

    @staticmethod
    def _focus_sessions(items: list[dict], warnings: list[str]) -> list[FocusSession]:
        sessions: list[FocusSession] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            normalized = {
                "task_title": item.get("task_title")
                or item.get("task")
                or item.get("title")
                or "Phiên tập trung",
                "objective": item.get("objective") or item.get("goal") or "Hoàn thành phần việc đã chọn.",
                "method": item.get("method")
                or item.get("approach")
                or "Làm theo từng phiên ngắn và kiểm tra kết quả.",
                "checklist": [
                    str(step)
                    for step in item.get("checklist", item.get("steps", []))
                    if str(step).strip()
                ],
            }
            try:
                sessions.append(FocusSession.model_validate(normalized))
            except Exception as exc:
                warnings.append(
                    f"Bỏ qua một focus session LLM không hợp lệ: {type(exc).__name__}: {exc}"
                )
        return sessions

    @staticmethod
    def _review_questions(items: list[dict], warnings: list[str]) -> list[ReviewQuestion]:
        questions: list[ReviewQuestion] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            normalized = {
                "question": item.get("question")
                or item.get("text")
                or item.get("review_question")
                or "Lịch này có phù hợp với năng lượng của bạn không?",
                "purpose": item.get("purpose") or item.get("why") or "Giúp điều chỉnh lịch cá nhân hóa hơn.",
            }
            try:
                questions.append(ReviewQuestion.model_validate(normalized))
            except Exception as exc:
                warnings.append(
                    f"Bỏ qua một review question LLM không hợp lệ: {type(exc).__name__}: {exc}"
                )
        return questions

    @staticmethod
    def _fallback_response(request: WorkflowRequest, exc: Exception) -> StructuredLLMResponse:
        tasks = [
            {
                "title": CalendarWorkflow._input_title(item),
                "deadline": item.deadline_at.date().isoformat()
                if item.deadline_mode == "specific" and item.deadline_at
                else None,
                "deadline_source": "Deadline do người dùng nhập"
                if item.deadline_mode == "specific" and item.deadline_at
                else None,
                "type": "general",
                "priority": "medium",
                "estimated_minutes": item.estimated_minutes,
                "deadline_mode": item.deadline_mode,
                "occurrences_per_day": 1,
            }
            for item in request.task_inputs
        ]
        if not tasks and request.raw_input.strip():
            tasks = [
                {
                    "title": title[:80],
                    "deadline": None,
                    "deadline_source": None,
                    "type": "general",
                    "priority": "medium",
                    "estimated_minutes": 30,
                    "deadline_mode": "none",
                    "occurrences_per_day": 1,
                }
                for title in re.split(r"\s+và\s+|,|\n", request.raw_input)
                if title.strip()
            ]
        data = {
            "tasks": tasks,
            "goal_analysis": {
                "summary": "Kế hoạch được tạo từ dữ liệu người dùng nhập khi LLM không sẵn sàng.",
                "success_criteria": [],
                "assumptions": ["Ưu tiên được đặt mức trung bình vì chưa có phân tích AI."],
            },
            "risks": [
                {
                    "title": "LLM tạm thời không khả dụng",
                    "severity": "medium",
                    "mitigation": "Có thể bấm cập nhật lại sau khi nhà cung cấp API ổn định.",
                }
            ],
            "focus_sessions": [],
            "review_questions": [],
            "suggested_subtasks": [],
        }
        raw_text = json.dumps({"fallback": True, "error": str(exc), **data}, ensure_ascii=False)
        return StructuredLLMResponse(data=data, raw_text=raw_text, provider="local-fallback")

    @staticmethod
    def _context(request: WorkflowRequest) -> str:
        return json.dumps({
            "task_text": request.raw_input or ", ".join(item.title for item in request.task_inputs),
            "structured_tasks": [item.model_dump(mode="json") for item in request.task_inputs],
            "additional_information": request.planning_notes,
            "calendar_rules": {
                "horizon_days": 14,
                "remaining_time": "fill daytime gaps with personal time and night gaps with rest",
                "conflicts": "higher AI priority wins; move lower priority work to another day",
                "scheduling_intents": (
                    "For every task, return a scheduling_intent. Infer recurrence, preferred_start_time, "
                    "preferred_period, occurrences_per_day, and target_weekday from the user's words. "
                    "Use null preferred_start_time when there is no explicit or strongly implied time. "
                    "These intents guide the scheduler; avoid hard-coded defaults unless evidence is weak."
                ),
            },
        }, ensure_ascii=False)

    @staticmethod
    def _validate_task(item: dict) -> Task:
        normalized = dict(item)
        if isinstance(normalized.get("deadline"), str) and "T" in normalized["deadline"]:
            normalized["deadline"] = normalized["deadline"].split("T", maxsplit=1)[0]
        return Task.model_validate(normalized)

    @staticmethod
    def _apply_inputs(tasks: list[Task], inputs: list[TaskInput], additional: str) -> list[Task]:
        if not inputs:
            return CalendarWorkflow._merge_inferred_tasks(tasks, additional)
        lookup = {task.title.casefold(): task for task in tasks}
        meal_match = re.search(r"(\d+)\s*bữa", additional.casefold())
        result = []
        for item in inputs:
            title = CalendarWorkflow._input_title(item)
            task = lookup.get(title.casefold()) or Task(title=title, estimated_minutes=item.estimated_minutes)
            task.title = title
            task.estimated_minutes = item.estimated_minutes
            task.deadline_mode = item.deadline_mode
            task.deadline = item.deadline_at.date() if item.deadline_mode == "specific" and item.deadline_at else None
            task.deadline_time = item.deadline_at.time() if item.deadline_mode == "specific" and item.deadline_at else None
            if task.deadline:
                task.deadline_source = "Deadline do người dùng nhập"
            if meal_match and re.search(r"nấu\s*cơm|nau\s*com", item.title.casefold()):
                task.occurrences_per_day = min(int(meal_match.group(1)), 6)
                if task.deadline_mode == "none":
                    task.deadline_mode = "daily"
            result.append(task)
        return CalendarWorkflow._merge_inferred_tasks(result, additional)

    @staticmethod
    def _merge_inferred_tasks(tasks: list[Task], text: str) -> list[Task]:
        inferred = CalendarWorkflow._infer_tasks_from_text(text)
        result = CalendarWorkflow._drop_note_like_tasks(list(tasks), inferred, text)
        existing = {task.title.casefold() for task in result}
        for task in inferred:
            if task.title.casefold() not in existing:
                result.append(task)
                existing.add(task.title.casefold())
        return result

    @staticmethod
    def _drop_note_like_tasks(tasks: list[Task], inferred: list[Task], text: str) -> list[Task]:
        normalized = text.casefold()
        inferred_titles = {task.title.casefold() for task in inferred}
        cleaned: list[Task] = []
        for task in tasks:
            title = task.title.casefold()
            if "nấu cơm" in inferred_titles and "nấu" in title and "bữa" in title:
                continue
            if "học tiếng anh" in inferred_titles and "tiếng anh" in title and "ưu tiên" in title:
                continue
            if "tập thể dục" in inferred_titles and "thể dục" in title:
                continue
            if "học toán" in inferred_titles and "học toán" in title:
                continue
            if "chơi game" in inferred_titles and ("chơi game" in title or "buổi chơi" in title):
                continue
            if "nấu cơm" in inferred_titles and "nấu cơm" in title:
                continue
            if "đi siêu thị" in inferred_titles and "siêu thị" in title:
                continue
            if "nghỉ làm" in title:
                continue
            if re.search(r"đi\s+làm|làm\s+việc", normalized) and re.search(r"đi\s+làm|làm\s+việc", title) and len(title) > 12:
                continue
            if any(value.startswith("đi ăn") for value in inferred_titles) and "đi ăn" in title and len(title) > 8:
                continue
            if any(value.startswith("đi chơi") for value in inferred_titles) and "đi chơi" in title and len(title) > 10:
                continue
            cleaned.append(task)
        return cleaned

    @staticmethod
    def _infer_tasks_from_text(text: str) -> list[Task]:
        normalized = text.casefold()
        inferred: list[Task] = []

        meal_match = re.search(r"(\d+)\s*bữa", normalized)
        if re.search(r"nấu\s*cơm|nau\s*com", normalized):
            inferred.append(
                Task(
                    title="Nấu cơm",
                    deadline_mode="daily" if meal_match or "ngày nào" in normalized or "mỗi ngày" in normalized else "none",
                    estimated_minutes=30,
                    occurrences_per_day=min(int(meal_match.group(1)), 6) if meal_match else 1,
                    type="housework",
                    priority="medium",
                )
            )

        if re.search(r"tiếng\s*anh|english", normalized) and not re.search(
            r"không\s+học|không\s+muốn\s+học", normalized
        ):
            inferred.append(
                Task(
                    title="Học tiếng Anh",
                    deadline_mode="daily" if "mỗi ngày" in normalized or "hằng ngày" in normalized else "none",
                    estimated_minutes=45,
                    type="learning",
                    priority="high" if "ưu tiên" in normalized else "medium",
                )
            )

        if re.search(r"dọn\s*nhà|don\s*nha", normalized):
            inferred.append(Task(title="Dọn nhà", estimated_minutes=45, type="housework"))

        if re.search(r"thể\s*dục|tập\s*thể\s*dục|exercise|workout", normalized):
            inferred.append(
                Task(
                    title="Tập thể dục",
                    deadline_mode="daily" if "mỗi ngày" in normalized or "ngày nào" in normalized else "none",
                    estimated_minutes=30,
                    type="health",
                    priority="high",
                )
            )

        if re.search(r"học\s*toán|toán", normalized):
            inferred.append(
                Task(
                    title="Học toán",
                    deadline_mode="daily" if "mỗi ngày" in normalized or "ngày nào" in normalized else "none",
                    estimated_minutes=CalendarWorkflow._duration_minutes(
                        CalendarWorkflow._event_segment(normalized, r"học\s*toán|toán"),
                        default=60,
                    ),
                    type="learning",
                    priority="high",
                )
            )

        if re.search(r"chơi\s*game|gaming", normalized):
            inferred.append(
                Task(
                    title="Chơi game",
                    deadline_mode="daily",
                    estimated_minutes=60,
                    occurrences_per_day=1,
                    type="leisure",
                    priority="low",
                )
            )

        if re.search(r"siêu\s*thị|mua\s*sắm|shopping", normalized):
            inferred.append(
                Task(
                    title="Đi siêu thị",
                    deadline_mode="none",
                    estimated_minutes=90,
                    type="errand",
                    priority="medium",
                )
            )

        if re.search(r"đi\s*ăn|ăn\s*(tối|trưa|sáng)|dinner|lunch|breakfast", normalized):
            segment = CalendarWorkflow._event_segment(
                normalized, r"đi\s*ăn|ăn\s*(tối|trưa|sáng)|dinner|lunch|breakfast"
            )
            inferred.append(
                Task(
                    title=CalendarWorkflow._event_title("Đi ăn", segment),
                    estimated_minutes=90 if re.search(r"ăn\s*tối|dinner", normalized) else 60,
                    type="personal_event",
                    priority="high",
                )
            )

        if re.search(r"đi\s*chơi|xem\s*phim|cà\s*phê|cafe|coffee|hang\s*out", normalized):
            segment = CalendarWorkflow._event_segment(
                normalized, r"đi\s*chơi|xem\s*phim|cà\s*phê|cafe|coffee|hang\s*out"
            )
            inferred.append(
                Task(
                    title=CalendarWorkflow._event_title("Đi chơi", segment),
                    estimated_minutes=120 if "xem phim" in normalized else 90,
                    type="personal_event",
                    priority="high",
                )
            )

        return inferred

    @staticmethod
    def _duration_minutes(text: str, default: int = 60) -> int:
        hour_match = re.search(r"(\d+)\s*(tiếng|giờ|hour)", text)
        if hour_match:
            return min(max(int(hour_match.group(1)) * 60, 5), 480)
        minute_match = re.search(r"(\d+)\s*(phút|minute)", text)
        if minute_match:
            return min(max(int(minute_match.group(1)), 5), 480)
        return default

    @staticmethod
    def _event_segment(text: str, pattern: str) -> str:
        for segment in re.split(r",|;|\n|\s+rồi\s+|\s+và\s+", text):
            if re.search(pattern, segment):
                return segment
        return text

    @staticmethod
    def _event_title(default: str, text: str) -> str:
        time_match = re.search(
            r"(\d{1,2})(?::|h| giờ)\s*(\d{1,2})?\s*(sáng|trưa|chiều|tối)?",
            text,
        )
        if not time_match:
            return default
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        suffix = time_match.group(3) or ""
        if suffix == "tối" and hour < 12:
            hour += 12
        if suffix == "chiều" and hour < 12:
            hour += 12
        return f"{default} lúc {hour:02d}:{minute:02d}"

    @staticmethod
    def _input_title(item: TaskInput) -> str:
        title = item.title.strip()
        if title:
            return title
        if item.deadline_mode == "daily":
            return "Việc hằng ngày"
        if item.deadline_mode == "specific" and item.deadline_at:
            return f"Việc có deadline {item.deadline_at:%d/%m/%Y %H:%M}"
        return "Việc cần sắp lịch"

    @staticmethod
    def _score(task: Task) -> int:
        return {"high": 85, "medium": 60, "low": 30}.get(task.priority, 60) + (10 if task.deadline else 0)

    @staticmethod
    def _reason(task: Task) -> str:
        return "AI đánh giá ưu tiên cao hơn và có deadline" if task.deadline else "AI đánh giá theo tác động mục tiêu"

    @staticmethod
    def _subtasks(tasks: list[Task], suggestions: list[dict]) -> dict[str, list[Subtask]]:
        result = {task.title: [] for task in tasks}
        titles = {task.title.casefold(): task.title for task in tasks}
        for item in suggestions:
            title = titles.get(str(item.get("task_title", "")).casefold())
            if title:
                result[title].append(Subtask.model_validate(item))
        return result

    @staticmethod
    def _scheduling_intents(items: list[dict], tasks: list[Task]) -> dict[str, dict]:
        titles = {task.title.casefold(): task.title for task in tasks}
        intents: dict[str, dict] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            raw_title = str(item.get("task_title") or "").casefold().strip()
            matched_title = titles.get(raw_title)
            if not matched_title:
                matched_title = next(
                    (title for key, title in titles.items() if raw_title and (raw_title in key or key in raw_title)),
                    None,
                )
            if not matched_title:
                continue
            preferred_period = str(item.get("preferred_period") or "flexible")
            recurrence = str(item.get("recurrence") or "once")
            try:
                occurrences_per_day = int(item.get("occurrences_per_day") or 1)
            except (TypeError, ValueError):
                occurrences_per_day = 1
            intents[matched_title.casefold()] = {
                "task_title": matched_title,
                "preferred_start_time": CalendarWorkflow._valid_time_string(
                    item.get("preferred_start_time")
                ),
                "preferred_period": preferred_period
                if preferred_period in {"morning", "afternoon", "evening", "night", "flexible"}
                else "flexible",
                "recurrence": recurrence
                if recurrence in {"once", "daily", "weekdays", "alternate_days", "specific_weekday"}
                else "once",
                "target_weekday": item.get("target_weekday")
                if isinstance(item.get("target_weekday"), int)
                else None,
                "occurrences_per_day": min(max(occurrences_per_day, 1), 6),
                "reason": str(item.get("reason") or ""),
            }
        return intents

    @staticmethod
    def _valid_time_string(value) -> str | None:
        text = str(value or "").strip()
        if re.fullmatch(r"\d{1,2}:\d{2}", text):
            hour, minute = [int(part) for part in text.split(":")]
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
        return None

    @classmethod
    def _build_calendar(
        cls,
        tasks: list[Task],
        request: WorkflowRequest,
        intents: dict[str, dict] | None = None,
    ) -> tuple[list[ScheduleSession], list[str]]:
        today = date.today()
        occupied: dict[date, list[tuple[int, int, Task | None]]] = {}
        warnings: list[str] = []
        task_blocks: list[ScheduleSession] = []
        intents = intents or {}
        notes = f"{request.raw_input}\n{request.planning_notes}".casefold()
        default_start = cls._time_to_minutes(request.daily_start_time)
        default_end = cls._time_to_minutes(request.daily_end_time)

        if re.search(r"đi\s+làm|đi\s+làm\s+việc|làm\s+việc", notes):
            for offset in range(14):
                day = today + timedelta(days=offset)
                if cls._is_workday(day, notes):
                    work_start = cls._work_start(notes)
                    work_minutes = cls._work_minutes(notes)
                    lunch_start, lunch_end = 12 * 60, 13 * 60
                    first_end = min(work_start + work_minutes, lunch_start)
                    remaining = work_minutes - max(0, first_end - work_start)
                    blocks = []
                    if first_end > work_start:
                        blocks.append((work_start, first_end))
                    if remaining > 0:
                        blocks.append((lunch_end, lunch_end + remaining))
                    occupied.setdefault(day, []).extend([(start, end, None) for start, end in blocks])
                    task_blocks.extend([
                        ScheduleSession(
                            date=day,
                            start_time=cls._clock(start),
                            end_time=cls._clock(end),
                            task_title="Đi làm",
                            minutes=end - start,
                            block_type="work",
                            period=cls._period_for(start),
                        )
                        for start, end in blocks
                    ])

        def add_task(task: Task, day: date, occurrence_index: int = 0) -> bool:
            max_end = 22 * 60
            if task.deadline and day == task.deadline and task.deadline_time:
                max_end = task.deadline_time.hour * 60 + task.deadline_time.minute
            busy = occupied.setdefault(day, [])
            intent = intents.get(task.title.casefold(), {})
            candidate = cls._best_slot(
                task,
                day,
                busy,
                max_end,
                notes,
                default_start,
                default_end,
                occurrence_index,
                intent,
            )
            if candidate:
                start, end = candidate
                busy.append((start, end, task))
                task_blocks.append(ScheduleSession(
                    date=day, start_time=cls._clock(start), end_time=cls._clock(end),
                    task_title=task.title, minutes=task.estimated_minutes, block_type="task",
                    period=cls._period_for(start),
                ))
                return True
            return False

        ordered = sorted(tasks, key=lambda task: (-cls._score(task), task.title))
        for task in ordered:
            intent = intents.get(task.title.casefold(), {})
            days = [today + timedelta(days=index) for index in range(14)]
            occurrences = cls._occurrences_for_task(task, intent)
            for day in days:
                if task.deadline and day > task.deadline:
                    break
                if (
                    task.deadline_mode != "daily"
                    and day != days[0]
                    and not cls._has_deferred_target_day(task, notes, intent)
                ):
                    continue
                if not cls._should_schedule_task_on_day(task, day, notes, intent):
                    continue
                for occurrence_index in range(occurrences):
                    if not add_task(task, day, occurrence_index):
                        if task.deadline_mode == "daily":
                            warnings.append(f"Không đủ chỗ cho lần lặp hằng ngày của '{task.title}' vào {day}.")
                        else:
                            for later in days[1:]:
                                if not task.deadline or later <= task.deadline:
                                    if add_task(task, later, occurrence_index):
                                        break
                            else:
                                warnings.append(f"Xung đột lịch: chưa xếp được '{task.title}'.")
                        break

        return cls._fill_default_blocks_clean(task_blocks), warnings

    @staticmethod
    def _work_start(notes: str) -> int:
        match = re.search(r"từ\s*(\d{1,2})(?:h| giờ)?\s*(sáng|chiều|tối)?", notes)
        if not match:
            return 8 * 60
        hour = int(match.group(1))
        suffix = match.group(2) or ""
        if suffix in {"chiều", "tối"} and hour < 12:
            hour += 12
        return hour * 60

    @staticmethod
    def _work_minutes(notes: str) -> int:
        match = re.search(r"đi\s*làm\s*(\d+)\s*(tiếng|giờ)", notes)
        return int(match.group(1)) * 60 if match else 8 * 60

    @staticmethod
    def _is_workday(day: date, notes: str) -> bool:
        if re.search(r"nghỉ\s*làm\s*thứ\s*7\s*chủ\s*nhật|nghỉ\s*làm\s*t7\s*cn", notes):
            return day.weekday() < 5
        return day.weekday() < 5

    @staticmethod
    def _occurrences_for_task(task: Task, intent: dict) -> int:
        if intent:
            return int(intent.get("occurrences_per_day") or 1)
        return task.occurrences_per_day if task.deadline_mode == "daily" else 1

    @staticmethod
    def _should_schedule_task_on_day(task: Task, day: date, notes: str, intent: dict | None = None) -> bool:
        intent = intent or {}
        recurrence = intent.get("recurrence")
        if recurrence == "once":
            return day == date.today()
        if recurrence == "daily":
            return True
        if recurrence == "weekdays":
            return day.weekday() < 5
        if recurrence == "alternate_days":
            return (day - date.today()).days % 2 == 0
        if recurrence == "specific_weekday":
            return day.weekday() == intent.get("target_weekday")
        title = task.title.casefold()
        if "chơi game" in title and re.search(r"buổi\s*chơi\s*buổi\s*không|cách\s*ngày", notes):
            return (day - date.today()).days % 2 == 0
        if "đi siêu thị" in title and re.search(r"thứ\s*7\s*này|t7\s*này", notes):
            return day.weekday() == 5 and 0 <= (day - date.today()).days <= 7
        return True

    @staticmethod
    def _has_deferred_target_day(task: Task, notes: str, intent: dict | None = None) -> bool:
        intent = intent or {}
        if intent.get("recurrence") == "specific_weekday":
            return True
        title = task.title.casefold()
        return "đi siêu thị" in title and bool(re.search(r"thứ\s*7\s*này|t7\s*này", notes))

    @classmethod
    def _best_slot(
        cls,
        task: Task,
        day: date,
        busy: list[tuple[int, int, Task | None]],
        max_end: int,
        notes: str,
        default_start: int,
        default_end: int,
        occurrence_index: int,
        intent: dict | None = None,
    ) -> tuple[int, int] | None:
        candidates: list[tuple[int, int, int]] = []
        intent = intent or {}
        anchors = cls._anchors_for(task, notes, default_start, default_end, intent)
        exact_start = cls._exact_start(task, intent)
        if exact_start is not None:
            exact_end = exact_start + task.estimated_minutes
            if exact_end <= max_end and exact_end <= DAY_END and not cls._overlaps(exact_start, exact_end, busy):
                return exact_start, exact_end
        if cls._occurrences_for_task(task, intent) > 1 and len(anchors) > occurrence_index:
            anchors = [anchors[occurrence_index]]
        for start in range(DAY_START, min(DAY_END, max_end) - task.estimated_minutes + 1, 15):
            end = start + task.estimated_minutes
            if end > max_end or cls._overlaps(start, end, busy):
                continue
            score = cls._slot_score(
                task,
                day,
                start,
                end,
                notes,
                anchors,
                default_start,
                default_end,
                intent,
            )
            candidates.append((score, start, end))
        if not candidates:
            return None
        _, start, end = max(candidates, key=lambda item: (item[0], -abs(item[1] - default_start), -item[1]))
        return start, end

    @staticmethod
    def _slot_score(
        task: Task,
        day: date,
        start: int,
        end: int,
        notes: str,
        anchors: list[int],
        default_start: int,
        default_end: int,
        intent: dict | None = None,
    ) -> int:
        intent = intent or {}
        score = 100
        title = task.title.casefold()
        midpoint = (start + end) // 2

        if anchors:
            score += max(0, 120 - min(abs(start - anchor) for anchor in anchors))
        elif default_start <= start and end <= max(default_end, default_start + task.estimated_minutes):
            score += 45

        preferred_period = intent.get("preferred_period")
        if preferred_period == "flexible":
            preferred_period = None
        preferred_period = preferred_period or CalendarWorkflow._preferred_period(notes, title)
        if preferred_period == "night" and start >= 21 * 60:
            score += 50
        elif preferred_period and CalendarWorkflow._period_for(start) == preferred_period:
            score += 50
        if "hạn chế việc nặng buổi tối" in notes and start >= 18 * 60 and task.estimated_minutes >= 60:
            score -= 80
        if "trì hoãn" in notes and start >= 20 * 60:
            score -= 35
        if task.deadline:
            days_left = (task.deadline - day).days
            score += max(0, 35 - days_left * 6)
        if start < 7 * 60 and not re.search(r"thuốc|nấu|bữa sáng", title):
            score -= 70
        if midpoint >= 21 * 60:
            score -= 30
        if task.estimated_minutes >= 90 and 12 * 60 <= start < 13 * 60:
            score -= 25
        return score

    @staticmethod
    def _anchors_for(
        task: Task,
        notes: str,
        default_start: int,
        default_end: int,
        intent: dict | None = None,
    ) -> list[int]:
        intent = intent or {}
        preferred_start = CalendarWorkflow._minutes_from_time_string(
            intent.get("preferred_start_time")
        )
        if preferred_start is not None:
            return [preferred_start]
        period = intent.get("preferred_period")
        if period and period != "flexible":
            period_anchors = {
                "morning": [8 * 60],
                "afternoon": [15 * 60],
                "evening": [19 * 60],
                "night": [21 * 60],
            }
            return period_anchors.get(period, [])
        title = task.title.casefold()
        if re.search(r"nấu\s*cơm|bữa", title):
            if "buổi chiều" in notes or "chiều" in notes:
                return [17 * 60]
            return [7 * 60, 11 * 60 + 30, 18 * 60]
        if re.search(r"thuốc", title):
            return [7 * 60 + 30, 12 * 60 + 30, 20 * 60]
        if re.search(r"học|ôn|tiếng\s*anh|toán|english", title):
            if "buổi sáng" in notes or "sáng" in notes:
                return [8 * 60]
            return [default_start if 7 * 60 <= default_start < DAY_END else 19 * 60]
        if re.search(r"thể\s*dục|tập\s*thể\s*dục", title):
            return [6 * 60]
        if re.search(r"chơi\s*game", title):
            return [20 * 60]
        if re.search(r"siêu\s*thị", title):
            return [9 * 60]
        if re.search(r"đọc|truyện|sách", title):
            return [20 * 60]
        if re.search(r"dọn|lau|giặt", title):
            return [16 * 60, 19 * 60]
        event_time = re.search(r"lúc\s*(\d{2}):(\d{2})", title)
        if event_time:
            return [int(event_time.group(1)) * 60 + int(event_time.group(2))]
        if re.search(r"đi\s*ăn|ăn\s*tối", title):
            return [18 * 60 + 30]
        if re.search(r"đi\s*chơi|xem\s*phim|cà\s*phê", title):
            return [19 * 60 + 30]
        return [default_start] if 7 * 60 <= default_start < DAY_END else [9 * 60, 15 * 60, 19 * 60]

    @staticmethod
    def _exact_start(task: Task, intent: dict | None = None) -> int | None:
        intent = intent or {}
        preferred_start = CalendarWorkflow._minutes_from_time_string(
            intent.get("preferred_start_time")
        )
        if preferred_start is not None:
            return preferred_start
        event_time = re.search(r"lúc\s*(\d{2}):(\d{2})", task.title.casefold())
        if not event_time:
            return None
        return int(event_time.group(1)) * 60 + int(event_time.group(2))

    @staticmethod
    def _minutes_from_time_string(value) -> int | None:
        text = str(value or "").strip()
        if not re.fullmatch(r"\d{2}:\d{2}", text):
            return None
        hour, minute = [int(part) for part in text.split(":")]
        return hour * 60 + minute

    @staticmethod
    def _preferred_period(notes: str, task_title: str) -> str | None:
        if "buổi sáng" in notes or "sáng" in notes:
            return "morning"
        if "buổi chiều" in notes or "chiều" in notes:
            return "afternoon"
        if "buổi tối" in notes or "tối" in notes:
            return "evening"
        if re.search(r"đọc|truyện|sách", task_title):
            return "evening"
        if re.search(r"học|ôn|tiếng\s*anh|toán", task_title):
            return "evening"
        return None

    @staticmethod
    def _overlaps(start: int, end: int, busy: list[tuple[int, int, Task | None]]) -> bool:
        return any(s < end and e > start for s, e, _ in busy)

    @staticmethod
    def _period_for(minutes: int) -> str:
        if minutes < 12 * 60:
            return "morning"
        if minutes < 18 * 60:
            return "afternoon"
        if minutes < 22 * 60:
            return "evening"
        return "night"

    @staticmethod
    def _time_to_minutes(value) -> int:
        return value.hour * 60 + value.minute

    @staticmethod
    def _fill_default_blocks(tasks: list[ScheduleSession], occupied: dict[date, list[tuple[int, int, Task]]]) -> list[ScheduleSession]:
        by_day: dict[date, list[ScheduleSession]] = {}
        for item in tasks:
            by_day.setdefault(item.date, []).append(item)
        result = []
        start_day = date.today()
        for offset in range(14):
            day = start_day + timedelta(days=offset)
            items = sorted(by_day.get(day, []), key=lambda item: item.start_time)
            cursor = 0
            for item in items:
                start = CalendarWorkflow._parse_time(item.start_time)
                if cursor < start:
                    result.append(ScheduleSession(date=day, start_time=CalendarWorkflow._clock(cursor), end_time=item.start_time, task_title="Thời gian cá nhân", minutes=start - cursor, block_type="rest" if cursor < 6 * 60 else "personal", period="night" if cursor < 6 * 60 else "morning"))
                result.append(item)
                cursor = CalendarWorkflow._parse_time(item.end_time)
            if cursor < 22 * 60:
                result.append(ScheduleSession(date=day, start_time=CalendarWorkflow._clock(cursor), end_time="22:00", task_title="Thời gian cá nhân", minutes=22 * 60 - cursor, block_type="personal", period="evening"))
            result.append(ScheduleSession(date=day, start_time="22:00", end_time="23:59", task_title="Nghỉ ngơi", minutes=119, block_type="rest", period="night"))
        return sorted(result, key=lambda item: (item.date, item.start_time))

    @staticmethod
    def _fill_default_blocks_clean(tasks: list[ScheduleSession]) -> list[ScheduleSession]:
        by_day: dict[date, list[ScheduleSession]] = {}
        for item in tasks:
            by_day.setdefault(item.date, []).append(item)
        result: list[ScheduleSession] = []
        for offset in range(14):
            day = date.today() + timedelta(days=offset)
            items = sorted(by_day.get(day, []), key=lambda item: item.start_time)
            cursor = 0

            def add_gap(start: int, end: int) -> None:
                CalendarWorkflow._append_gaps(result, day, start, end)
                return
                if start < 6 * 60:
                    rest_end = min(end, 6 * 60)
                    if rest_end - start >= 5:
                        result.append(ScheduleSession(date=day, start_time=CalendarWorkflow._clock(start), end_time=CalendarWorkflow._clock(rest_end), task_title="Nghỉ ngơi", minutes=rest_end - start, block_type="rest", period="night"))
                    start = rest_end
                if end - start >= 5:
                    period = "morning" if start < 12 * 60 else "afternoon" if start < 18 * 60 else "evening"
                    result.append(ScheduleSession(date=day, start_time=CalendarWorkflow._clock(start), end_time=CalendarWorkflow._clock(end), task_title="Thời gian cá nhân", minutes=end - start, block_type="personal", period=period))

            for item in items:
                start = CalendarWorkflow._parse_time(item.start_time)
                if cursor < start:
                    add_gap(cursor, start)
                result.append(item)
                cursor = CalendarWorkflow._parse_time(item.end_time)
            if cursor < 22 * 60:
                add_gap(cursor, 22 * 60)
            result.append(ScheduleSession(date=day, start_time="22:00", end_time="23:59", task_title="Nghỉ ngơi", minutes=119, block_type="rest", period="night"))
        return sorted(result, key=lambda item: (item.date, item.start_time))

    @staticmethod
    def _append_gaps(result: list[ScheduleSession], day: date, start: int, end: int) -> None:
        while start < end:
            if start < 6 * 60:
                chunk_end = min(end, 6 * 60)
                block_type, title, period = "rest", "Nghỉ ngơi", "night"
            else:
                chunk_end = min(end, 12 * 60 if start < 12 * 60 else 18 * 60 if start < 18 * 60 else 22 * 60)
                block_type, title = "personal", "Thời gian cá nhân"
                period = "morning" if start < 12 * 60 else "afternoon" if start < 18 * 60 else "evening"
            if chunk_end - start >= 5:
                result.append(ScheduleSession(date=day, start_time=CalendarWorkflow._clock(start), end_time=CalendarWorkflow._clock(chunk_end), task_title=title, minutes=chunk_end - start, block_type=block_type, period=period))
            start = chunk_end

    @staticmethod
    def _parse_time(value: str) -> int:
        hours, minutes = value.split(":")
        return int(hours) * 60 + int(minutes)

    @staticmethod
    def _clock(minutes: int) -> str:
        return f"{minutes // 60:02d}:{minutes % 60:02d}"
