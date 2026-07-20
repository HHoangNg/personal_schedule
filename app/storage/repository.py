import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.memory.embeddings import VoyageEmbedder
from app.memory.qdrant import QdrantMemory
from app.schemas import (
    CalendarEvent,
    DecisionAudit,
    BehaviorProfile,
    DailyFeedback,
    ExecutionLog,
    PlanSummary,
    ProductivityAnalytics,
    ScheduleProposal,
    WorkflowResult,
)

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
        self.feedback_directory = self.database_path.parent / "feedback"
        self.feedback_directory.mkdir(parents=True, exist_ok=True)
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
                "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '4') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS execution_logs (
                    log_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                    block_id TEXT NOT NULL, task_title TEXT NOT NULL, scheduled_date TEXT NOT NULL,
                    scheduled_start_time TEXT NOT NULL, scheduled_end_time TEXT NOT NULL,
                    status TEXT NOT NULL, actual_minutes INTEGER, reason TEXT NOT NULL,
                    recorded_at TEXT NOT NULL, UNIQUE(user_id, plan_id, block_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS daily_feedback (
                    feedback_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, feedback_date TEXT NOT NULL,
                    energy INTEGER NOT NULL, focus INTEGER NOT NULL, effective_period TEXT NOT NULL,
                    schedule_feeling TEXT NOT NULL, procrastinated_tasks_json TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(user_id, feedback_date)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_execution_user_date ON execution_logs(user_id, scheduled_date)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_user_date ON daily_feedback(user_id, feedback_date)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS decision_audits (
                    decision_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, plan_id TEXT,
                    action TEXT NOT NULL, message TEXT NOT NULL, provider TEXT NOT NULL,
                    confidence REAL NOT NULL, requires_confirmation INTEGER NOT NULL,
                    reasoning TEXT NOT NULL, applied INTEGER NOT NULL, created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_decisions_user_date ON decision_audits(user_id, created_at DESC)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schedule_proposals (
                    proposal_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, plan_id TEXT NOT NULL,
                    trigger_name TEXT NOT NULL, risk TEXT NOT NULL, requires_confirmation INTEGER NOT NULL,
                    applied INTEGER NOT NULL, changes_json TEXT NOT NULL, reasoning TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_proposals_user_date ON schedule_proposals(user_id, created_at DESC)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS calendar_sync_state (
                    user_id TEXT NOT NULL, provider TEXT NOT NULL, last_synced_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, provider)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS calendar_events (
                    user_id TEXT NOT NULL, provider TEXT NOT NULL, event_id TEXT NOT NULL,
                    summary TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
                    updated_at TEXT, attendee_count INTEGER NOT NULL, is_cancelled INTEGER NOT NULL,
                    etag TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, provider, event_id)
                )"""
            )
            self._ensure_column(connection, "decision_audits", "policy", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "decision_audits", "risk", "TEXT NOT NULL DEFAULT 'medium'")
            self._ensure_column(connection, "decision_audits", "change_summary", "TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    def record_execution(self, log: ExecutionLog) -> ExecutionLog:
        payload = log.model_dump(mode="json")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT log_id FROM execution_logs WHERE user_id = ? AND plan_id = ? AND block_id = ?",
                (log.user_id, log.plan_id, log.block_id),
            ).fetchone()
            log_id = existing["log_id"] if existing else log.log_id
            payload["log_id"] = log_id
            connection.execute(
                """INSERT INTO execution_logs(
                    log_id,user_id,plan_id,block_id,task_title,scheduled_date,scheduled_start_time,
                    scheduled_end_time,status,actual_minutes,reason,recorded_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id,plan_id,block_id) DO UPDATE SET
                    log_id=excluded.log_id,status=excluded.status,actual_minutes=excluded.actual_minutes,
                    reason=excluded.reason,recorded_at=excluded.recorded_at""",
                (payload["log_id"], log.user_id, log.plan_id, log.block_id, log.task_title,
                 payload["scheduled_date"], log.scheduled_start_time, log.scheduled_end_time,
                 log.status, log.actual_minutes, log.reason, payload["recorded_at"]),
            )
        (self.feedback_directory / f"execution_{payload['log_id']}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        self._sync_memory(f"execution:{log.user_id}:{payload['log_id']}", payload)
        return ExecutionLog.model_validate(payload)

    def save_daily_feedback(self, feedback: DailyFeedback) -> DailyFeedback:
        payload = feedback.model_dump(mode="json")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT feedback_id FROM daily_feedback WHERE user_id = ? AND feedback_date = ?",
                (feedback.user_id, payload["feedback_date"]),
            ).fetchone()
            feedback_id = existing["feedback_id"] if existing else feedback.feedback_id
            payload["feedback_id"] = feedback_id
            connection.execute(
                """INSERT INTO daily_feedback(
                    feedback_id,user_id,feedback_date,energy,focus,effective_period,
                    schedule_feeling,procrastinated_tasks_json,note,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id,feedback_date) DO UPDATE SET
                    feedback_id=excluded.feedback_id,energy=excluded.energy,focus=excluded.focus,
                    effective_period=excluded.effective_period,schedule_feeling=excluded.schedule_feeling,
                    procrastinated_tasks_json=excluded.procrastinated_tasks_json,note=excluded.note,
                    created_at=excluded.created_at""",
                (payload["feedback_id"], feedback.user_id, payload["feedback_date"], feedback.energy,
                 feedback.focus, feedback.effective_period, feedback.schedule_feeling,
                 json.dumps(feedback.procrastinated_tasks, ensure_ascii=False), feedback.note,
                 payload["created_at"]),
            )
        (self.feedback_directory / f"daily_{feedback.user_id}_{payload['feedback_date']}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        self._sync_memory(f"feedback:{feedback.user_id}:{payload['feedback_date']}", payload)
        return DailyFeedback.model_validate(payload)

    def record_decision(self, decision: DecisionAudit) -> DecisionAudit:
        payload = decision.model_dump(mode="json")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO decision_audits(
                    decision_id,user_id,plan_id,action,message,provider,confidence,
                    requires_confirmation,reasoning,applied,created_at,policy,risk,change_summary
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    plan_id=excluded.plan_id,applied=excluded.applied,
                    reasoning=excluded.reasoning,created_at=excluded.created_at,
                    policy=excluded.policy,risk=excluded.risk,change_summary=excluded.change_summary""",
                (
                    decision.decision_id, decision.user_id, decision.plan_id,
                    decision.action, decision.message, decision.provider,
                    decision.confidence, int(decision.requires_confirmation),
                    decision.reasoning, int(decision.applied), payload["created_at"],
                    decision.policy, decision.risk, decision.change_summary,
                ),
            )
        self._sync_memory(f"decision:{decision.user_id}:{decision.decision_id}", payload)
        return decision

    def list_decisions(self, user_id: str, limit: int = 50) -> list[DecisionAudit]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM decision_audits WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [
            DecisionAudit(
                decision_id=row["decision_id"], user_id=row["user_id"], plan_id=row["plan_id"],
                action=row["action"], message=row["message"], provider=row["provider"],
                confidence=row["confidence"], requires_confirmation=bool(row["requires_confirmation"]),
                reasoning=row["reasoning"], applied=bool(row["applied"]),
                policy=row["policy"] if "policy" in row.keys() else "",
                risk=row["risk"] if "risk" in row.keys() else "medium",
                change_summary=row["change_summary"] if "change_summary" in row.keys() else "",
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def search_memory(self, user_id: str, text: str, limit: int = 5) -> list[str]:
        if not self.qdrant_sync_enabled or not self.qdrant_url or not self.qdrant_collection or not self.voyage_api_key:
            return []
        try:
            memory = QdrantMemory(self.qdrant_url, self.qdrant_collection, self.qdrant_vector_name, self.qdrant_api_key)
            vector = VoyageEmbedder(self.voyage_api_key, self.voyage_model).embed(text)
            points = memory.search(vector, limit=limit)
            result = []
            for point in points:
                payload = point.payload or {}
                owner = payload.get("user_id") or (payload.get("plan_input") or {}).get("user_id")
                if owner == user_id:
                    result.append(json.dumps(payload, ensure_ascii=False, sort_keys=True)[:1200])
            return result
        except Exception:
            return []

    def _execution_rows(self, user_id: str):
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM execution_logs WHERE user_id = ? ORDER BY scheduled_date DESC",
                (user_id,),
            ).fetchall()

    def _feedback_rows(self, user_id: str):
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM daily_feedback WHERE user_id = ? ORDER BY feedback_date DESC",
                (user_id,),
            ).fetchall()

    def behavior_profile(self, user_id: str) -> BehaviorProfile:
        executions = self._execution_rows(user_id)
        feedback = self._feedback_rows(user_id)
        completed = sum(row["status"] in {"completed", "partial"} for row in executions)
        delayed = sum(row["status"] in {"skipped", "rescheduled"} for row in executions)
        duration_ratios = []
        for row in executions:
            if row["actual_minutes"] is None:
                continue
            start_h, start_m = [int(value) for value in row["scheduled_start_time"].split(":")]
            end_h, end_m = [int(value) for value in row["scheduled_end_time"].split(":")]
            scheduled_minutes = max(1, (end_h * 60 + end_m) - (start_h * 60 + start_m))
            duration_ratios.append(row["actual_minutes"] / scheduled_minutes)
        period_scores: defaultdict[str, list[float]] = defaultdict(list)
        for row in feedback:
            if row["effective_period"] != "flexible":
                period_scores[row["effective_period"]].append((row["energy"] + row["focus"]) / 2)
        effective = "chưa đủ dữ liệu"
        scores = {key: round(sum(values) / len(values), 2) for key, values in period_scores.items()}
        if scores:
            effective = max(scores, key=scores.get)
        skipped = Counter(row["task_title"] for row in executions if row["status"] in {"skipped", "rescheduled"})
        priority_adjustments = {title: max(-15, -count * 3) for title, count in skipped.most_common(20)}
        evidence = {title: [f"Đã bỏ qua hoặc dời {count} lần trong lịch sử"] for title, count in skipped.items()}
        return BehaviorProfile(
            user_id=user_id,
            sample_days=len({row["scheduled_date"] for row in executions} | {row["feedback_date"] for row in feedback}),
            execution_count=len(executions),
            completion_rate=round(completed / len(executions), 3) if executions else 0.0,
            procrastination_rate=round(delayed / len(executions), 3) if executions else 0.0,
            average_duration_ratio=round(sum(duration_ratios) / len(duration_ratios), 3) if duration_ratios else None,
            effective_period=effective,
            effective_period_scores=scores,
            commonly_skipped_tasks=[title for title, _ in skipped.most_common(5)],
            priority_adjustments=priority_adjustments,
            priority_evidence=evidence,
            enough_data_for_auto_apply=len(feedback) >= 7 or len(executions) >= 5,
        )

    def analytics(self, user_id: str) -> ProductivityAnalytics:
        profile = self.behavior_profile(user_id)
        executions = self._execution_rows(user_id)
        duration_errors = []
        manual_changes = 0
        for row in executions:
            if row["status"] == "rescheduled":
                manual_changes += 1
            if row["actual_minutes"] is None:
                continue
            start_h, start_m = [int(value) for value in row["scheduled_start_time"].split(":")]
            end_h, end_m = [int(value) for value in row["scheduled_end_time"].split(":")]
            duration_errors.append(abs(row["actual_minutes"] - ((end_h * 60 + end_m) - (start_h * 60 + start_m))))
        with self._connect() as connection:
            decision_rows = connection.execute(
                "SELECT action, applied FROM decision_audits WHERE user_id = ?", (user_id,)
            ).fetchall()
        suggestion_rows = [row for row in decision_rows if row["action"] in {"apply_suggestion", "schedule_proposal"}]
        accepted = sum(bool(row["applied"]) for row in suggestion_rows)
        return ProductivityAnalytics(
            user_id=user_id,
            profile=profile,
            metrics={
                "completion_rate": profile.completion_rate,
                "procrastination_rate": profile.procrastination_rate,
                "execution_count": profile.execution_count,
                "sample_days": profile.sample_days,
                "duration_mae_minutes": round(sum(duration_errors) / len(duration_errors), 2) if duration_errors else None,
                "manual_schedule_changes": manual_changes,
                "suggestion_acceptance_rate": round(accepted / len(suggestion_rows), 3) if suggestion_rows else None,
            },
        )

    def set_block_lock(self, user_id: str, plan_id: str, block_id: str, locked: bool, reason: str) -> WorkflowResult:
        plan = self.get_for_user(user_id, plan_id)
        if not plan:
            raise KeyError("Không tìm thấy kế hoạch.")
        found = False
        updated_schedule = []
        for block in plan.schedule:
            if block.block_id == block_id:
                found = True
                updated_schedule.append(block.model_copy(update={"is_locked": locked, "lock_reason": reason if locked else ""}))
            else:
                updated_schedule.append(block)
        if not found:
            raise KeyError("Không tìm thấy khối lịch.")
        return self.save(user_id, plan.model_copy(update={"schedule": updated_schedule}))

    def save_proposal(self, proposal: ScheduleProposal) -> ScheduleProposal:
        payload = proposal.model_dump(mode="json")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO schedule_proposals(
                    proposal_id,user_id,plan_id,trigger_name,risk,requires_confirmation,
                    applied,changes_json,reasoning,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(proposal_id) DO UPDATE SET applied=excluded.applied,changes_json=excluded.changes_json""",
                (
                    proposal.proposal_id, proposal.user_id, proposal.plan_id, proposal.trigger,
                    proposal.risk, int(proposal.requires_confirmation), int(proposal.applied),
                    json.dumps(payload["changes"], ensure_ascii=False), proposal.reasoning,
                    payload["created_at"],
                ),
            )
        return proposal

    def save_calendar_events(self, user_id: str, provider: str, events: list[CalendarEvent]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            for event in events:
                payload = event.model_dump(mode="json")
                connection.execute(
                    """INSERT INTO calendar_events(
                        user_id,provider,event_id,summary,start_at,end_at,updated_at,attendee_count,
                        is_cancelled,etag,last_seen_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(user_id,provider,event_id) DO UPDATE SET
                        summary=excluded.summary,start_at=excluded.start_at,end_at=excluded.end_at,
                        updated_at=excluded.updated_at,attendee_count=excluded.attendee_count,
                        is_cancelled=excluded.is_cancelled,etag=excluded.etag,last_seen_at=excluded.last_seen_at""",
                    (
                        user_id, provider, event.event_id, event.summary, payload["start_at"],
                        payload["end_at"], payload["updated_at"], event.attendee_count,
                        int(event.is_cancelled), event.etag, now,
                    ),
                )
            connection.execute(
                """INSERT INTO calendar_sync_state(user_id,provider,last_synced_at) VALUES (?,?,?)
                ON CONFLICT(user_id,provider) DO UPDATE SET last_synced_at=excluded.last_synced_at""",
                (user_id, provider, now),
            )

    def delete_user_data(self, user_id: str) -> int:
        if self.qdrant_sync_enabled and self.qdrant_url and self.qdrant_collection and self.qdrant_api_key:
            try:
                QdrantMemory(
                    self.qdrant_url, self.qdrant_collection,
                    self.qdrant_vector_name, self.qdrant_api_key,
                ).delete_by_user(user_id)
            except Exception:
                pass
        with self._connect() as connection:
            plan_ids = [row["plan_id"] for row in connection.execute("SELECT plan_id FROM plans WHERE user_id = ?", (user_id,))]
            connection.execute("DELETE FROM execution_logs WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM daily_feedback WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM decision_audits WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM schedule_proposals WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM calendar_events WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM calendar_sync_state WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM plans WHERE user_id = ?", (user_id,))
        for plan_id in plan_ids:
            (self.json_directory / f"{plan_id}.json").unlink(missing_ok=True)
        for file in self.feedback_directory.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                if data.get("user_id") == user_id:
                    file.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                continue
        return len(plan_ids)

    def _sync_memory(self, point_id: str, payload: dict) -> None:
        if not self.qdrant_sync_enabled or not self.qdrant_url or not self.qdrant_collection or not self.voyage_api_key:
            return
        try:
            memory = QdrantMemory(self.qdrant_url, self.qdrant_collection, self.qdrant_vector_name, self.qdrant_api_key)
            memory.ensure_collection(self.qdrant_vector_size)
            vector = VoyageEmbedder(self.voyage_api_key, self.voyage_model).embed(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            if len(vector) == self.qdrant_vector_size:
                memory.upsert(str(uuid5(NAMESPACE_URL, point_id)), vector, payload)
        except Exception:
            # Primary SQLite/JSON writes must remain available when Qdrant is down.
            return

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
            payload = json.loads(row["payload_json"])
            payload, changed = self._migrate_payload(plan_id, payload)
            plan = WorkflowResult.model_validate(payload)
            if changed:
                self._persist_payload(plan_id, user_id, plan)
            return plan
        except Exception:
            repaired = self._repair_payload(row["payload_json"])
            repaired, changed = self._migrate_payload(plan_id, repaired)
            plan = WorkflowResult.model_validate(repaired)
            if changed:
                self._persist_payload(plan_id, user_id, plan)
            return plan

    def update_block_status(
        self, user_id: str, plan_id: str, block_id: str, status: str,
        actual_minutes: int | None = None, reason: str = "",
    ) -> tuple[WorkflowResult, ExecutionLog]:
        plan = self.get_for_user(user_id, plan_id)
        if not plan:
            raise KeyError("Không tìm thấy kế hoạch.")
        block = next((item for item in plan.schedule if item.block_id == block_id), None)
        if not block:
            raise KeyError("Không tìm thấy khối lịch.")
        updated_block = block.model_copy(update={
            "status": status, "actual_minutes": actual_minutes,
            "status_note": reason,
        })
        updated_schedule = [updated_block if item.block_id == block_id else item for item in plan.schedule]
        saved = self.save(user_id, plan.model_copy(update={"schedule": updated_schedule}))
        log = self.record_execution(ExecutionLog(
            user_id=user_id, plan_id=saved.plan_id or plan_id, block_id=block_id,
            task_title=block.task_title, scheduled_date=block.date,
            scheduled_start_time=block.start_time, scheduled_end_time=block.end_time,
            status=status, actual_minutes=actual_minutes, reason=reason,
        ))
        return saved, log

    def _persist_payload(self, plan_id: str, user_id: str, plan: WorkflowResult) -> None:
        payload = json.dumps(plan.model_dump(mode="json"), ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                "UPDATE plans SET payload_json = ? WHERE user_id = ? AND plan_id = ?",
                (payload, user_id, plan_id),
            )
        (self.json_directory / f"{plan_id}.json").write_text(payload, encoding="utf-8")

    @staticmethod
    def _migrate_payload(plan_id: str, payload: dict) -> tuple[dict, bool]:
        changed = False
        payload.setdefault("plan_id", plan_id)
        for index, block in enumerate(payload.get("schedule") or []):
            if not isinstance(block, dict):
                continue
            if not block.get("block_id"):
                stable_key = ":".join(
                    [
                        "schedule-block",
                        plan_id,
                        str(index),
                        str(block.get("date") or ""),
                        str(block.get("start_time") or ""),
                        str(block.get("end_time") or ""),
                        str(block.get("task_title") or ""),
                    ]
                )
                block["block_id"] = str(uuid5(NAMESPACE_URL, stable_key))
                changed = True
            if not block.get("status"):
                block["status"] = "planned"
                changed = True
        return payload, changed

    @staticmethod
    def _repair_payload(payload_json: str) -> dict:
        payload = json.loads(payload_json)
        plan_input = payload.get("plan_input")
        if isinstance(plan_input, dict):
            notes = str(plan_input.get("planning_notes") or "")
            if len(notes) > MAX_STORED_NOTE_CHARS:
                plan_input["planning_notes"] = notes[-MAX_STORED_NOTE_CHARS:]
        return payload
