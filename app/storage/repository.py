import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.memory.embeddings import VoyageEmbedder
from app.memory.qdrant import QdrantMemory
from app.schemas import PlanSummary, WorkflowResult

MAX_STORED_NOTE_CHARS = 1000


class PlanRepository:
    """Small SQLite repository so a saved plan survives browser refresh and restarts."""

    def __init__(
        self,
        database_path: str,
        qdrant_url: str | None = None,
        qdrant_api_key: str | None = None,
        qdrant_collection: str | None = None,
        qdrant_vector_size: int = 768,
        qdrant_vector_name: str = "dense",
        qdrant_sync_enabled: bool = False,
        voyage_api_key: str | None = None,
        voyage_model: str = "voyage-3",
    ):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.json_directory = self.database_path.parent / "plans"
        self.json_directory.mkdir(parents=True, exist_ok=True)
        self.qdrant_url = qdrant_url
        self.qdrant_api_key = qdrant_api_key
        self.qdrant_collection = qdrant_collection
        self.qdrant_vector_size = qdrant_vector_size
        self.qdrant_vector_name = qdrant_vector_name
        self.qdrant_sync_enabled = qdrant_sync_enabled
        self.voyage_api_key = voyage_api_key
        self.voyage_model = voyage_model
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    task_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_plans_user_created ON plans(user_id, created_at DESC)"
            )

    def save(self, user_id: str, plan: WorkflowResult) -> WorkflowResult:
        with self._connect() as connection:
            current = connection.execute(
                "SELECT plan_id FROM plans WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        plan_id = current["plan_id"] if current else str(uuid4())
        created_at = datetime.now(timezone.utc)
        saved_plan = plan.model_copy(update={"plan_id": plan_id, "created_at": created_at})
        tasks = saved_plan.tasks
        title = tasks[0].title if tasks else "Kế hoạch chưa có công việc"
        payload_dict = saved_plan.model_dump(mode="json")
        qdrant_warning = self._sync_qdrant(plan_id, payload_dict)
        if qdrant_warning:
            saved_plan = saved_plan.model_copy(
                update={
                    "warnings": [*saved_plan.warnings, qdrant_warning],
                    "needs_confirmation": True,
                }
            )
            payload_dict = saved_plan.model_dump(mode="json")
        payload = json.dumps(payload_dict, ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO plans(plan_id, user_id, created_at, title, task_count, payload_json) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(plan_id) DO UPDATE SET user_id=excluded.user_id, created_at=excluded.created_at, "
                "title=excluded.title, task_count=excluded.task_count, payload_json=excluded.payload_json",
                (plan_id, user_id, created_at.isoformat(), title, len(tasks), payload),
            )
            connection.execute("DELETE FROM plans WHERE user_id = ? AND plan_id != ?", (user_id, plan_id))
        (self.json_directory / f"{plan_id}.json").write_text(payload, encoding="utf-8")
        return saved_plan

    def _sync_qdrant(self, plan_id: str, payload: dict) -> str | None:
        if not self.qdrant_sync_enabled:
            return None
        if not self.qdrant_url or not self.qdrant_collection:
            return "Chưa cấu hình Qdrant nên lịch chỉ được lưu vào SQLite/JSON."
        if not self.voyage_api_key:
            return "Chưa cấu hình VOYAGE_API_KEY nên chưa thể embed dữ liệu để lưu lên Qdrant."
        try:
            memory = QdrantMemory(
                self.qdrant_url,
                self.qdrant_collection,
                vector_name=self.qdrant_vector_name,
                api_key=self.qdrant_api_key or None,
            )
            memory.ensure_collection(self.qdrant_vector_size)
            vector_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            vector = VoyageEmbedder(self.voyage_api_key, self.voyage_model).embed(vector_text)
            if len(vector) != self.qdrant_vector_size:
                return (
                    "Kích thước embedding Voyage không khớp Qdrant: "
                    f"Voyage trả {len(vector)}, QDRANT_VECTOR_SIZE={self.qdrant_vector_size}."
                )
            memory.upsert(plan_id, vector, payload)
        except Exception as exc:
            return f"Không đồng bộ được Qdrant: {type(exc).__name__}: {exc}"
        return None

    def list_for_user(self, user_id: str, limit: int = 1) -> list[PlanSummary]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT plan_id, created_at, title, task_count FROM plans WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [
            PlanSummary(
                plan_id=row["plan_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
                title=row["title"],
                task_count=row["task_count"],
            )
            for row in rows
        ]

    def get_for_user(self, user_id: str, plan_id: str) -> WorkflowResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM plans WHERE user_id = ? AND plan_id = ?",
                (user_id, plan_id),
            ).fetchone()
        if not row:
            return None
        try:
            return WorkflowResult.model_validate_json(row["payload_json"])
        except Exception:
            repaired = self._repair_payload(row["payload_json"])
            return WorkflowResult.model_validate(repaired)

    @staticmethod
    def _repair_payload(payload_json: str) -> dict:
        payload = json.loads(payload_json)
        plan_input = payload.get("plan_input")
        if isinstance(plan_input, dict):
            notes = str(plan_input.get("planning_notes") or "")
            if len(notes) > MAX_STORED_NOTE_CHARS:
                plan_input["planning_notes"] = notes[-MAX_STORED_NOTE_CHARS:]
        return payload
