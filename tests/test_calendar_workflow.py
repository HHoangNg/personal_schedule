from datetime import date

from app.core.settings import Settings
from app.llm.providers import StructuredLLMResponse
from app.schemas import TaskInput, WorkflowRequest
from app.workflow.calendar_workflow import CalendarWorkflow


class BrokenProvider:
    def generate_json(self, prompt, schema):
        raise RuntimeError("503 UNAVAILABLE")


class IntentProvider:
    def __init__(self, tasks: list[dict], intents: list[dict]):
        self.tasks = tasks
        self.intents = intents

    def generate_json(self, prompt, schema):
        return StructuredLLMResponse(
            data={
                "tasks": self.tasks,
                "scheduling_intents": self.intents,
                "goal_analysis": {
                    "summary": "AI đã phân biệt yêu cầu và chọn lịch theo ngữ cảnh.",
                    "success_criteria": [],
                    "assumptions": [],
                },
                "risks": [],
                "focus_sessions": [],
                "review_questions": [],
                "suggested_subtasks": [],
            },
            raw_text="{}",
            provider="test-ai",
        )


def test_additional_information_creates_three_daily_meal_blocks():
    request = WorkflowRequest(
        user_id="calendar-test",
        task_inputs=[TaskInput(title="Nấu cơm", estimated_minutes=30)],
        planning_notes="Nấu cơm 3 bữa mỗi ngày",
    )
    result = CalendarWorkflow(Settings(llm_provider="mock")).run(request)
    cooking = [item for item in result.schedule if item.task_title == "Nấu cơm"]

    assert len({item.date for item in cooking}) == 14
    assert all(item.minutes == 30 for item in cooking)
    assert len(cooking) == 42


def test_calendar_still_updates_when_llm_provider_is_unavailable(monkeypatch):
    monkeypatch.setattr("app.workflow.calendar_workflow.build_provider", lambda settings: BrokenProvider())
    request = WorkflowRequest(
        user_id="fallback-user",
        task_inputs=[TaskInput(title="Há»c tiáº¿ng Anh", estimated_minutes=60)],
    )

    result = CalendarWorkflow(Settings(llm_provider="gemini", gemini_api_key="fake")).run(request)

    assert result.llm_provider == "local-fallback"
    assert result.tasks[0].title == "Há»c tiáº¿ng Anh"
    assert any(block.task_title == "Há»c tiáº¿ng Anh" for block in result.schedule)
    assert any("LLM" in warning for warning in result.warnings)


def test_daily_learning_uses_user_window_instead_of_defaulting_to_6am():
    request = WorkflowRequest(
        user_id="learning-window",
        task_inputs=[TaskInput(title="Học tiếng Anh", deadline_mode="daily", estimated_minutes=60)],
    )

    result = CalendarWorkflow(Settings(llm_provider="mock")).run(request)
    learning_blocks = [item for item in result.schedule if item.task_title == "Học tiếng Anh"]

    assert len(learning_blocks) == 14
    assert all(item.start_time == "19:00" for item in learning_blocks)


def test_notes_only_english_priority_creates_morning_learning_block():
    request = WorkflowRequest(
        user_id="notes-english",
        planning_notes="tuần này tôi muốn ưu tiên tiếng Anh buổi sáng",
    )

    result = CalendarWorkflow(Settings(llm_provider="mock")).run(request)
    learning_blocks = [item for item in result.schedule if item.task_title == "Học tiếng Anh"]

    assert learning_blocks
    assert learning_blocks[0].start_time == "08:00"


def test_personal_events_from_notes_are_not_rendered_as_personal_time():
    request = WorkflowRequest(
        user_id="personal-events",
        planning_notes="19h đi ăn với bạn, 20h30 đi chơi",
    )

    result = CalendarWorkflow(Settings(llm_provider="mock")).run(request)
    event_titles = [item.task_title for item in result.schedule if item.block_type == "task"]

    assert any(title.startswith("Đi ăn") for title in event_titles)
    assert any(title.startswith("Đi chơi") for title in event_titles)
    event_day = next(item.date for item in result.schedule if item.task_title.startswith("Đi ăn"))
    assert not any(
        item.date == event_day
        and item.task_title == "Thời gian cá nhân"
        and item.start_time <= "19:00" < item.end_time
        for item in result.schedule
    )


def test_complex_lifestyle_notes_become_specific_calendar_blocks():
    request = WorkflowRequest(
        user_id="complex-notes",
        planning_notes=(
            "tôi thích thể dục sáng sớm, đi làm 7 tiếng từ 8 giờ sáng mỗi ngày, "
            "nghỉ làm thứ 7 chủ nhật, buổi tối hay chơi game, buổi chơi buổi không, "
            "ngày nào cũng nấu cơm buổi chiều, ngày nào cũng học toán 1 tiếng, "
            "thứ 7 này tôi đi siêu thị"
        ),
    )

    result = CalendarWorkflow(Settings(llm_provider="mock")).run(request)
    blocks = [item for item in result.schedule if item.block_type in {"task", "work"}]

    assert any(item.task_title == "Tập thể dục" and item.start_time == "06:00" for item in blocks)
    assert any(item.task_title == "Đi làm" and item.start_time == "08:00" and item.end_time == "12:00" for item in blocks)
    assert any(item.task_title == "Đi làm" and item.start_time == "13:00" and item.end_time == "16:00" for item in blocks)
    assert not any(item.task_title == "Đi làm" and item.date.weekday() >= 5 for item in blocks)
    assert any(item.task_title == "Nấu cơm" and item.start_time == "17:00" for item in blocks)
    assert any(item.task_title == "Học toán" and item.minutes == 60 for item in blocks)
    assert any(item.task_title == "Đi siêu thị" and item.date.weekday() == 5 for item in blocks)


def test_ai_scheduling_intent_overrides_rule_based_learning_default(monkeypatch):
    provider = IntentProvider(
        tasks=[
            {
                "title": "Học toán",
                "deadline": None,
                "deadline_source": None,
                "type": "learning",
                "priority": "high",
                "estimated_minutes": 60,
                "deadline_mode": "daily",
                "occurrences_per_day": 1,
            }
        ],
        intents=[
            {
                "task_title": "Học toán",
                "preferred_start_time": "15:00",
                "preferred_period": "afternoon",
                "recurrence": "daily",
                "target_weekday": None,
                "occurrences_per_day": 1,
                "reason": "AI chọn buổi chiều vì người dùng học tốt sau giờ nghỉ.",
            }
        ],
    )
    monkeypatch.setattr("app.workflow.calendar_workflow.build_provider", lambda settings: provider)

    result = CalendarWorkflow(Settings(llm_provider="openai", openai_api_key="fake")).run(
        WorkflowRequest(user_id="ai-intent", raw_input="Ngày nào cũng học toán 1 tiếng")
    )

    math_blocks = [item for item in result.schedule if item.task_title == "Học toán"]
    assert len(math_blocks) == 14
    assert all(item.start_time == "15:00" for item in math_blocks)


def test_ai_scheduling_intent_controls_alternate_day_recurrence(monkeypatch):
    provider = IntentProvider(
        tasks=[
            {
                "title": "Chơi game",
                "deadline": None,
                "deadline_source": None,
                "type": "personal",
                "priority": "low",
                "estimated_minutes": 60,
                "deadline_mode": "daily",
                "occurrences_per_day": 1,
            }
        ],
        intents=[
            {
                "task_title": "Chơi game",
                "preferred_start_time": "20:00",
                "preferred_period": "evening",
                "recurrence": "alternate_days",
                "target_weekday": None,
                "occurrences_per_day": 1,
                "reason": "AI hiểu 'buổi chơi buổi không' là cách ngày.",
            }
        ],
    )
    monkeypatch.setattr("app.workflow.calendar_workflow.build_provider", lambda settings: provider)

    result = CalendarWorkflow(Settings(llm_provider="openai", openai_api_key="fake")).run(
        WorkflowRequest(user_id="ai-alternate", raw_input="Buổi tối hay chơi game, buổi chơi buổi không")
    )

    game_blocks = [item for item in result.schedule if item.task_title == "Chơi game"]
    assert game_blocks
    assert all((item.date - date.today()).days % 2 == 0 for item in game_blocks)
    assert all(item.start_time == "20:00" for item in game_blocks)
