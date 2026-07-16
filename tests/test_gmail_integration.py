from app.core.settings import Settings
from app.integrations.gmail import GmailMessage, GmailScheduleImporter
from app.llm.providers import StructuredLLMResponse


class FakeGmailClient:
    def scan_recent(self, days: int = 3, max_results: int = 50):
        return [
            GmailMessage(
                message_id="m1",
                sender="teacher@example.com",
                subject="Lịch học tiếng Anh tối thứ 5",
                date="Wed, 15 Jul 2026 08:00:00 +0700",
                snippet="Lớp học diễn ra lúc 19:00.",
            ),
            GmailMessage(
                message_id="m2",
                sender="promo@example.com",
                subject="Khuyến mãi cuối tuần",
                date="Wed, 15 Jul 2026 09:00:00 +0700",
                snippet="Mua hàng giảm giá.",
            ),
            GmailMessage(
                message_id="m3",
                sender="friend@example.com",
                subject="19h đi ăn tối",
                date="Wed, 15 Jul 2026 10:00:00 +0700",
                snippet="Tối nay 19h đi ăn rồi đi chơi.",
            ),
        ]


class ManyLongGmailClient:
    def scan_recent(self, days: int = 3, max_results: int = 50):
        long_snippet = "Lịch họp lúc 19:00. " * 80
        promo = "Khuyến mãi giảm giá sale voucher. " * 80
        messages = [
            GmailMessage(
                message_id=f"meeting-{index}",
                sender="calendar@example.com",
                subject=f"Lịch họp dự án {index}",
                date="Wed, 15 Jul 2026 08:00:00 +0700",
                snippet=long_snippet,
            )
            for index in range(20)
        ]
        messages.append(
            GmailMessage(
                message_id="promo",
                sender="promo@example.com",
                subject="Khuyến mãi lịch sale cuối tuần",
                date="Wed, 15 Jul 2026 09:00:00 +0700",
                snippet=promo,
            )
        )
        return messages


class FakeAIProvider:
    def generate_json(self, prompt, schema):
        data = {
            "summary": "AI đã phân loại email.",
            "classified_messages": [
                {
                    "message_id": "promo",
                    "is_schedule_related": False,
                    "schedule_note": "",
                    "reason": "Đây là thư quảng cáo dù có từ lịch.",
                    "confidence": 0.95,
                },
                {
                    "message_id": "real-event",
                    "is_schedule_related": True,
                    "schedule_note": "Thứ 6 19:00 đi ăn với An.",
                    "reason": "Có lời hẹn gặp và thời gian cụ thể.",
                    "confidence": 0.92,
                },
            ],
            "relevant_notes": [],
            "ignored_message_ids": ["promo"],
        }
        return StructuredLLMResponse(data, "{}", "fake-ai")


def test_gmail_importer_builds_calendar_request_from_relevant_messages():
    importer = GmailScheduleImporter(Settings(llm_provider="mock"), client=FakeGmailClient())

    request = importer.build_request(
        user_id="gmail-user",
        display_name="Minh",
        days=3,
        max_results=50,
    )

    assert request.user_id == "gmail-user"
    assert request.display_name == "Minh"
    assert "Gmail 3" in request.planning_notes
    assert "Lịch học tiếng Anh" in request.raw_input
    assert "đi ăn" in request.raw_input
    assert "Khuyến mãi" not in request.raw_input


def test_gmail_importer_caps_long_notes_and_ignores_promotions():
    importer = GmailScheduleImporter(Settings(llm_provider="mock"), client=ManyLongGmailClient())

    request = importer.build_request(
        user_id="gmail-long-user",
        display_name="Minh",
        days=3,
        max_results=50,
    )

    assert len(request.raw_input) <= 950
    assert len(request.planning_notes) <= 950
    assert "Khuyến mãi" not in request.raw_input


def test_gmail_importer_uses_ai_classification_not_keyword_list(monkeypatch):
    importer = GmailScheduleImporter(Settings(llm_provider="mock"), client=FakeGmailClient())
    importer.provider = FakeAIProvider()

    request = importer.build_request(
        user_id="gmail-ai-user",
        display_name="Minh",
        days=3,
        max_results=50,
    )

    assert "đi ăn với An" in request.raw_input
    assert "Khuyến mãi" not in request.raw_input
    assert "Lịch học tiếng Anh" not in request.raw_input
