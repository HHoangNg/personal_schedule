from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from app.schemas import CalendarEvent
from app.security.paths import require_project_subpath


GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


@dataclass(frozen=True)
class CalendarConnectorConfig:
    credentials_path: str
    token_path: str


class GoogleCalendarConnector:
    """Read-only Google Calendar connector. It never writes external events."""

    provider = "google"

    def __init__(self, config: CalendarConnectorConfig):
        self.credentials_path = require_project_subpath(
            config.credentials_path, "secrets", "Google Calendar credentials"
        )
        self.token_path = require_project_subpath(
            config.token_path, "data", "Google Calendar token"
        )

    def list_events(self, days: int = 14, max_results: int = 250) -> list[CalendarEvent]:
        service = self._service(interactive=False)
        now = datetime.now(timezone.utc)
        response = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days)).isoformat(),
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return [self._event(item) for item in response.get("items", []) if item.get("id")]

    def authorize_interactively(self) -> None:
        """Explicit user-initiated OAuth flow; never run during background sync."""
        self._service(interactive=True)

    def _service(self, interactive: bool):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        credentials = None
        if self.token_path.exists():
            credentials = Credentials.from_authorized_user_file(
                str(self.token_path), GOOGLE_CALENDAR_SCOPES
            )
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            elif interactive:
                if not self.credentials_path.is_file():
                    raise FileNotFoundError(
                        "Không tìm thấy Google Calendar OAuth client trong secrets/."
                    )
                credentials = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), GOOGLE_CALENDAR_SCOPES
                ).run_local_server(port=0)
            else:
                raise PermissionError(
                    "Google Calendar chưa được kết nối. Hãy thực hiện endpoint connect do người dùng khởi tạo."
                )
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(credentials.to_json(), encoding="utf-8")
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    @staticmethod
    def _event(item: dict) -> CalendarEvent:
        def parse(value: dict) -> datetime:
            raw = value.get("dateTime") or f"{value.get('date')}T00:00:00+00:00"
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))

        return CalendarEvent(
            event_id=item["id"],
            summary=str(item.get("summary") or ""),
            start_at=parse(item.get("start") or {}),
            end_at=parse(item.get("end") or {}),
            updated_at=datetime.fromisoformat(item["updated"].replace("Z", "+00:00")) if item.get("updated") else None,
            attendee_count=len(item.get("attendees") or []),
            is_cancelled=item.get("status") == "cancelled",
            etag=str(item.get("etag") or ""),
        )
