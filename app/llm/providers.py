import json
import re
from dataclasses import dataclass
from typing import Any, Protocol


SYSTEM_PROMPT = """You are a Vietnamese personal productivity coach and planning engine.
Return JSON only and obey the supplied response schema exactly. The user message is JSON with
task_text and verified_constraints. Use Vietnamese in every natural-language field.
Create a practical, personalized plan: analyse the goal, decompose work into small verifiable
steps, surface risks, propose focused work methods, and ask useful review questions.
The deadline_at in verified_constraints is the only verified structured deadline. A task deadline
must be YYYY-MM-DD only, never include a time. Do not invent deadlines; use null for a task
deadline when there is no explicit evidence. Treat energy, work
style, daily window, commitments, and notes as constraints, not decoration. Keep facts separate
from recommendations and state uncertainty as an assumption."""


@dataclass
class StructuredLLMResponse:
    data: dict[str, Any]
    raw_text: str
    provider: str


class LLMProvider(Protocol):
    def generate_json(self, prompt: str, schema: dict[str, Any]) -> StructuredLLMResponse: ...


class MockProvider:
    def generate_json(self, prompt: str, schema: dict[str, Any]) -> StructuredLLMResponse:
        try:
            context = json.loads(prompt)
            if "messages" in context and "relevant_notes" in schema.get("properties", {}):
                classified, relevant_notes, ignored = self._mock_gmail_analysis(
                    context.get("messages", [])
                )
                data = {
                    "summary": f"Tìm thấy {len(relevant_notes)} email có khả năng liên quan lịch trình.",
                    "classified_messages": classified,
                    "relevant_notes": relevant_notes,
                    "ignored_message_ids": ignored,
                }
                return StructuredLLMResponse(data, json.dumps(data, ensure_ascii=False), "mock")
            structured = context.get("structured_tasks", [])
            prompt = ", ".join(item.get("title", "") for item in structured) or context.get("task_text", prompt)
        except (json.JSONDecodeError, AttributeError):
            pass
        titles = [part.strip().capitalize() for part in re.split(r"\s+và\s+|,|\n", prompt) if part.strip()]
        tasks = [
            {
                "title": title[:80],
                "deadline": None,
                "deadline_source": None,
                "type": "general",
                "priority": "medium",
                "estimated_minutes": 30,
            }
            for title in titles
        ]
        return StructuredLLMResponse({"tasks": tasks}, json.dumps({"tasks": tasks}), "mock")

    @staticmethod
    def _mock_gmail_analysis(messages: list[dict]) -> tuple[list[dict], list[str], list[str]]:
        relevant_terms = re.compile(
            r"lịch|họp|hẹn|deadline|class|meeting|event|flight|ăn|chơi|cà phê|coffee|dinner|lunch",
            re.I,
        )
        noise_terms = re.compile(r"khuyến mãi|giảm giá|sale|voucher|newsletter|unsubscribe|quảng cáo", re.I)
        notes: list[str] = []
        ignored: list[str] = []
        classified: list[dict] = []
        for message in messages:
            text = f"{message.get('subject', '')}\n{message.get('snippet', '')}"
            if relevant_terms.search(text) and not noise_terms.search(text):
                note = (
                    "Email Gmail liên quan lịch trình: "
                    f"{message.get('subject', '')} | từ {message.get('sender', '')} | "
                    f"ngày {message.get('date', '')} | {message.get('snippet', '')}"
                )
                notes.append(note)
                classified.append(
                    {
                        "message_id": str(message.get("message_id", "")),
                        "is_schedule_related": True,
                        "schedule_note": note,
                        "reason": "Có dấu hiệu cuộc hẹn/lịch trình.",
                        "confidence": 0.9,
                    }
                )
            else:
                ignored.append(str(message.get("message_id", "")))
                classified.append(
                    {
                        "message_id": str(message.get("message_id", "")),
                        "is_schedule_related": False,
                        "schedule_note": "",
                        "reason": "Không đủ bằng chứng là lịch trình.",
                        "confidence": 0.8,
                    }
                )
        return classified, notes, ignored


class OpenAIProvider:
    def __init__(self, api_key: str, model: str):
        from openai import OpenAI

        self.client, self.model = OpenAI(api_key=api_key), model

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> StructuredLLMResponse:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content or "{}"
        return StructuredLLMResponse(json.loads(text), text, "openai")


class GeminiProvider:
    def __init__(self, api_key: str, model: str):
        from google import genai

        self.client, self.model = genai.Client(api_key=api_key), model

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> StructuredLLMResponse:
        response = self.client.models.generate_content(
            model=self.model,
            contents=f"{SYSTEM_PROMPT}\n{prompt}",
            config={"response_mime_type": "application/json", "response_schema": schema},
        )
        text = response.text or "{}"
        return StructuredLLMResponse(json.loads(text), text, "gemini")


class CompareProvider:
    def __init__(self, openai_provider: LLMProvider, gemini_provider: LLMProvider):
        self.openai_provider = openai_provider
        self.gemini_provider = gemini_provider

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> StructuredLLMResponse:
        responses: list[StructuredLLMResponse] = []
        errors: list[str] = []
        for provider in (self.openai_provider, self.gemini_provider):
            try:
                responses.append(provider.generate_json(prompt, schema))
            except Exception as exc:
                errors.append(f"{provider.__class__.__name__}: {type(exc).__name__}: {exc}")
        if not responses:
            raise RuntimeError("; ".join(errors))
        primary = next((item for item in responses if item.provider == "openai"), responses[0])
        raw_text = json.dumps(
            {
                "selected_provider": primary.provider,
                "providers": [
                    {"provider": item.provider, "data": item.data, "raw_text": item.raw_text}
                    for item in responses
                ],
                "errors": errors,
            },
            ensure_ascii=False,
        )
        data = dict(primary.data)
        if errors:
            data.setdefault("risks", []).append(
                {
                    "title": "Một provider LLM không phản hồi trong chế độ đối chiếu",
                    "severity": "medium",
                    "mitigation": "; ".join(errors),
                }
            )
        return StructuredLLMResponse(data, raw_text, "openai+gemini")


def build_provider(settings) -> LLMProvider:
    provider = settings.llm_provider.casefold()
    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        return OpenAIProvider(settings.openai_api_key, settings.openai_model)
    if provider == "gemini":
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
        return GeminiProvider(settings.gemini_api_key, settings.gemini_model)
    if provider in {"both", "compare", "openai+gemini", "gpt+gemini"}:
        if not settings.openai_api_key or not settings.gemini_api_key:
            raise ValueError("OPENAI_API_KEY and GEMINI_API_KEY are required when LLM_PROVIDER=compare")
        return CompareProvider(
            OpenAIProvider(settings.openai_api_key, settings.openai_model),
            GeminiProvider(settings.gemini_api_key, settings.gemini_model),
        )
    return MockProvider()
