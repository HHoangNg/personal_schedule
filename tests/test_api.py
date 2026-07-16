from fastapi.testclient import TestClient

from app.core.settings import Settings, get_settings
from app.api.routes import _merge_notes
from app.main import app


def mock_settings():
    return Settings(llm_provider="mock", qdrant_sync_enabled=False)


app.dependency_overrides[get_settings] = mock_settings


def test_health():
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_dashboard_is_vietnamese():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "Lịch cá nhân của bạn" in response.text


def test_plan_uses_schema_and_returns_confirmation_flag():
    response = TestClient(app).post(
        "/v1/workflow/plan",
        json={"user_id": "u1", "raw_input": "Hoàn thành README"},
    )
    assert response.status_code == 200
    body = response.json()
    assert {"tasks", "schedule", "confidence", "warnings", "llm_provider"} <= body.keys()
    assert body["llm_provider"] == "mock"


def test_schedule_sessions_do_not_overlap():
    response = TestClient(app).post(
        "/v1/workflow/plan",
        json={"user_id": "u1", "raw_input": "học toán và đọc truyện"},
    )
    assert response.status_code == 200
    schedule = response.json()["schedule"]
    assert schedule[0]["end_time"] == schedule[1]["start_time"]


def test_structured_tasks_and_name_are_saved():
    response = TestClient(app).post(
        "/v1/workflow/plan",
        json={
            "user_id": "u-structured",
            "display_name": "Nguyễn Minh",
            "task_inputs": [
                {"title": "Học tiếng Anh", "type": "learning", "estimated_minutes": 35, "deadline_mode": "daily"},
                {"title": "Dọn nhà", "type": "housework", "estimated_minutes": 30, "deadline_mode": "specific", "deadline_at": "2026-07-20T20:00:00"},
            ],
            "available_minutes_per_day": 120,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert [task["title"] for task in body["tasks"]] == ["Học tiếng Anh", "Dọn nhà"]
    assert body["plan_input"]["display_name"] == "Nguyễn Minh"
    daily_dates = {item["date"] for item in body["schedule"] if item["task_title"] == "Học tiếng Anh"}
    assert len(daily_dates) == 14


def test_same_user_updates_one_existing_calendar():
    first = TestClient(app).post(
        "/v1/workflow/plan",
        json={"user_id": "one-calendar", "task_inputs": [{"title": "Đọc sách", "estimated_minutes": 30}]},
    ).json()
    second = TestClient(app).post(
        "/v1/workflow/plan",
        json={"user_id": "one-calendar", "task_inputs": [{"title": "Đi làm", "estimated_minutes": 60}], "planning_notes": "Đi làm"},
    ).json()
    saved = TestClient(app).get("/v1/plans?user_id=one-calendar").json()

    assert first["plan_id"] == second["plan_id"]
    assert len(saved) == 1
    assert second["tasks"][0]["title"] == "Đi làm"
    assert any(block["block_type"] == "work" for block in second["schedule"])


def test_planning_notes_only_can_update_calendar():
    response = TestClient(app).post(
        "/v1/workflow/plan",
        json={
            "user_id": "notes-only",
            "display_name": "Minh",
            "planning_notes": "\u0110i l\u00e0m",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["plan_input"]["display_name"] == "Minh"
    assert any(block["block_type"] == "work" for block in body["schedule"])

def test_deadline_only_input_can_create_task():
    response = TestClient(app).post(
        "/v1/workflow/plan",
        json={
            "user_id": "deadline-only-user",
            "task_inputs": [
                {
                    "title": "",
                    "estimated_minutes": 45,
                    "deadline_mode": "specific",
                    "deadline_at": "2026-07-20T10:00:00",
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tasks"][0]["title"]
    assert any(block["block_type"] == "task" for block in body["schedule"])


def test_merge_notes_deduplicates_and_caps_length():
    previous = "Yêu cầu riêng trước đó:\n" + ("thích học sáng\n" * 120)
    updated = "Cập nhật mới:\nthích học sáng"

    merged = _merge_notes(previous, updated)

    assert len(merged) <= 950
    assert "Yêu cầu riêng trước đó:" not in merged
    assert "Cập nhật mới:" not in merged
