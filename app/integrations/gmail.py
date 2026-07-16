import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.llm.providers import build_provider
from app.schemas import WorkflowRequest


GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MAX_WORKFLOW_TEXT_CHARS = 950
MAX_GMAIL_NOTES = 8
MAX_GMAIL_NOTE_CHARS = 240
SCHEDULE_KEYWORDS = (
    "calendar",
    "breakfast",
    "class",
    "coffee",
    "deadline",
    "dinner",
    "due",
    "event",
    "flight",
    "lunch",
    "hạn",
    "hẹn",
    "họp",
    "ăn",
    "chơi",
    "cà phê",
    "interview",
    "lịch",
    "meeting",
    "reservation",
    "schedule",
    "thi",
    "workshop",
)
NON_SCHEDULE_KEYWORDS = (
    "khuyến mãi",
    "giảm giá",
    "sale",
    "voucher",
    "newsletter",
    "unsubscribe",
    "quảng cáo",
    "promotion",
)

GMAIL_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "classified_messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "is_schedule_related": {"type": "boolean"},
                    "schedule_note": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "message_id",
                    "is_schedule_related",
                    "schedule_note",
                    "reason",
                    "confidence",
                ],
            },
        },
        "relevant_notes": {"type": "array", "items": {"type": "string"}},
        "ignored_message_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "classified_messages", "relevant_notes", "ignored_message_ids"],
}


@dataclass
class GmailMessage:
    message_id: str
    sender: str
    subject: str
    date: str
    snippet: str


class GmailClient:
    """Reads recent Gmail metadata/snippets with a read-only OAuth token."""

    def __init__(self, credentials_path: str, token_path: str):
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)

    def scan_recent(self, days: int = 3, max_results: int = 50) -> list[GmailMessage]:
        service = self._service()
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=f"newer_than:{days}d",
                maxResults=max_results,
            )
            .execute()
        )
        messages = response.get("messages", [])
        return [self._get_message(service, item["id"]) for item in messages]

    def _service(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        credentials = None
        if self.token_path.exists():
            credentials = Credentials.from_authorized_user_file(str(self.token_path), GMAIL_SCOPES)
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                if not self.credentials_path.exists():
                    raise FileNotFoundError(
                        f"Không tìm thấy Gmail credentials tại {self.credentials_path}"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), GMAIL_SCOPES
                )
                credentials = flow.run_local_server(port=0)
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(credentials.to_json(), encoding="utf-8")
        return build("gmail", "v1", credentials=credentials)

    @staticmethod
    def _get_message(service, message_id: str) -> GmailMessage:
        message = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata", metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {
            item["name"].casefold(): item.get("value", "")
            for item in message.get("payload", {}).get("headers", [])
        }
        return GmailMessage(
            message_id=message_id,
            sender=headers.get("from", ""),
            subject=headers.get("subject", ""),
            date=headers.get("date", ""),
            snippet=message.get("snippet", ""),
        )


class GmailScheduleImporter:
    """Turns recent Gmail messages into planning notes for the calendar workflow."""

    def __init__(self, settings, client: GmailClient | None = None):
        self.settings = settings
        self.client = client or GmailClient(settings.gmail_credentials_path, settings.gmail_token_path)
        self.provider = build_provider(settings)

    def build_request(self, user_id: str, display_name: str, days: int, max_results: int) -> WorkflowRequest:
        messages = self.client.scan_recent(days=days, max_results=max_results)
        notes, warnings = self._extract_schedule_notes(messages, days)
        if not notes:
            notes = [f"Không tìm thấy email liên quan lịch trình trong {days} ngày gần nhất."]
        notes = self._compact_notes(notes)
        warnings = self._compact_notes(warnings, limit=3)
        planning_notes = "\n".join(
            [
                f"Nguồn: Gmail {days} ngày gần nhất.",
                *notes,
                *warnings,
            ]
        )
        raw_input = self._cap_text("\n".join(notes))
        planning_notes = self._cap_text(planning_notes)
        return WorkflowRequest(
            user_id=user_id,
            display_name=display_name,
            raw_input=raw_input,
            planning_notes=planning_notes,
            horizon_days=14,
        )

    def _extract_schedule_notes(
        self, messages: list[GmailMessage], days: int
    ) -> tuple[list[str], list[str]]:
        if not messages:
            return [], [f"Gmail không có email nào trong {days} ngày gần nhất."]
        prompt = json.dumps(
            {
                "instruction": (
                    "Bạn là bộ phân loại Gmail cho lịch cá nhân. Đọc từng email và quyết định "
                    "email đó có tạo/cập nhật lịch trình không. Chỉ đánh dấu is_schedule_related=true "
                    "nếu email chứa cuộc hẹn, deadline, lớp học, công việc cần làm theo thời gian, "
                    "chuyến đi, lời mời gặp mặt/ăn/chơi, hoặc thông tin có thể chuyển thành block lịch. "
                    "Bỏ qua quảng cáo, newsletter, thông báo hệ thống, hóa đơn hoặc mail mơ hồ. "
                    "schedule_note phải ngắn, tiếng Việt, chỉ chứa sự kiện/việc cần đưa vào lịch."
                ),
                "messages": [message.__dict__ for message in messages],
            },
            ensure_ascii=False,
        )
        try:
            response = self.provider.generate_json(prompt, GMAIL_ANALYSIS_SCHEMA)
            notes = self._notes_from_ai_response(response.data)
            if notes:
                return notes, []
        except Exception as exc:
            return self._local_filter(messages), [
                f"LLM không phân tích được Gmail nên dùng bộ lọc cục bộ: {type(exc).__name__}: {exc}"
            ]
        return self._local_filter(messages), ["LLM không tìm thấy email lịch trình rõ ràng."]

    @staticmethod
    def _notes_from_ai_response(data: dict[str, Any]) -> list[str]:
        notes: list[str] = []
        for item in data.get("classified_messages", []):
            if not isinstance(item, dict):
                continue
            if not item.get("is_schedule_related"):
                continue
            if float(item.get("confidence") or 0) < 0.55:
                continue
            note = str(item.get("schedule_note") or "").strip()
            if note:
                notes.append(note)
        if notes:
            return notes
        return [
            str(item).strip()
            for item in data.get("relevant_notes", [])
            if str(item).strip()
        ]

    @staticmethod
    def _local_filter(messages: list[GmailMessage]) -> list[str]:
        pattern = re.compile("|".join(re.escape(keyword) for keyword in SCHEDULE_KEYWORDS), re.I)
        noise = re.compile("|".join(re.escape(keyword) for keyword in NON_SCHEDULE_KEYWORDS), re.I)
        notes = []
        for message in messages:
            haystack = f"{message.subject}\n{message.snippet}"
            if pattern.search(haystack) and not noise.search(haystack):
                notes.append(
                    "Email Gmail liên quan lịch trình: "
                    f"{message.subject} | từ {message.sender} | ngày {message.date} | "
                    f"{GmailScheduleImporter._cap_text(message.snippet, MAX_GMAIL_NOTE_CHARS)}"
                )
        return notes

    @staticmethod
    def _compact_notes(notes: list[str], limit: int = MAX_GMAIL_NOTES) -> list[str]:
        compacted: list[str] = []
        seen: set[str] = set()
        for note in notes:
            cleaned = " ".join(str(note).split())
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            compacted.append(GmailScheduleImporter._cap_text(cleaned, MAX_GMAIL_NOTE_CHARS))
            if len(compacted) >= limit:
                break
        return compacted

    @staticmethod
    def _cap_text(value: str, limit: int = MAX_WORKFLOW_TEXT_CHARS) -> str:
        text = str(value).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "…"
