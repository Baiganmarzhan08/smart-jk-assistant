import asyncio
from base64 import b64decode
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientConnectionError, ClientResponseError

from bot.assistant import (
    UNKNOWN_RESTORATION_TIME,
    UNKNOWN_RESTORATION_TIME_KK,
    AssistantUnavailable,
    GeminiAssistant,
)
from bot.catalog import DEMO_CATALOG


@pytest.fixture
def gemini():
    session = MagicMock()
    assistant = GeminiAssistant(session, "test-key", "test-model")
    request = session.post.return_value
    return assistant, session, request


def test_gemini_request_and_answer(gemini):
    assistant, session, request = gemini
    request.__aenter__.return_value.json.return_value = {
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [
                {"text": "internal", "thought": True},
                {"text": "В каталоге есть сантехник."},
            ]},
        }],
    }

    assert asyncio.run(assistant.answer("  Есть сантехник?")) == "В каталоге есть сантехник."
    args, kwargs = session.post.call_args
    assert args == ("https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent",)
    assert kwargs["headers"] == {"x-goog-api-key": "test-key"}
    assert kwargs["timeout"].total == 20
    assert kwargs["allow_redirects"] is False
    payload = kwargs["json"]
    assert DEMO_CATALOG in payload["systemInstruction"]["parts"][0]["text"]
    assert "подтверждённой информации о сроке восстановления нет" in payload["systemInstruction"]["parts"][0]["text"]
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "  Есть сантехник?"}]}]
    assert "tools" not in payload


def test_transcription_sends_ogg_inline(gemini):
    assistant, session, request = gemini
    request.__aenter__.return_value.json.return_value = {
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": "Сәлем! Есть сантехник?"}]},
        }],
    }
    audio = b"OggS audio test"
    assert asyncio.run(assistant.transcribe(audio)) == "Сәлем! Есть сантехник?"
    options = session.post.call_args.kwargs
    assert options["timeout"].total == 35
    media = options["json"]["contents"][0]["parts"][0]["inlineData"]
    assert media["mimeType"] == "audio/ogg"
    assert b64decode(media["data"], validate=True) == audio
    assert DEMO_CATALOG not in options["json"]["systemInstruction"]["parts"][0]["text"]


@pytest.mark.parametrize("text", ["NO_SPEECH", "а" * 1501])
def test_transcription_rejects_unusable_text(gemini, text):
    assistant, _, request = gemini
    request.__aenter__.return_value.json.return_value = {
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}],
    }
    with pytest.raises(AssistantUnavailable):
        asyncio.run(assistant.transcribe(b"OggS audio test"))


@pytest.mark.parametrize(("question", "answer"), [
    ("Когда включат свет в подъезде?", UNKNOWN_RESTORATION_TIME),
    ("Во сколько дадут воду?", UNKNOWN_RESTORATION_TIME),
    ("Отопление когда восстановят?", UNKNOWN_RESTORATION_TIME),
    ("Кіреберістегі жарық қашан қосылады?", UNKNOWN_RESTORATION_TIME_KK),
    ("Сәлеметсіз бе! Біздің подъезде свет жоқ. Когда его включат?", UNKNOWN_RESTORATION_TIME),
    ("Сәлем, жарық жоқ. Когда включат?", UNKNOWN_RESTORATION_TIME),
    ("Подъезде свет жоқ. Қашан қосылады?", UNKNOWN_RESTORATION_TIME_KK),
])
def test_restoration_questions_use_confirmed_information(gemini, question, answer):
    assistant, session, _ = gemini
    assert asyncio.run(assistant.answer(question)) == answer
    session.post.assert_not_called()


@pytest.mark.parametrize("error", [
    TimeoutError(),
    ClientConnectionError(),
    ClientResponseError(None, (), status=429),
    ClientResponseError(None, (), status=403),
])
def test_gemini_transport_errors(gemini, error):
    assistant, _, request = gemini
    request.__aenter__.side_effect = error
    with pytest.raises(AssistantUnavailable):
        asyncio.run(assistant.answer("Вопрос"))


@pytest.mark.parametrize("result", [
    {"promptFeedback": {"blockReason": "SAFETY"}},
    {"candidates": [{"finishReason": "MAX_TOKENS"}]},
    {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "  "}]}}]},
])
def test_gemini_unusable_response(gemini, result):
    assistant, _, request = gemini
    request.__aenter__.return_value.json.return_value = result
    with pytest.raises(AssistantUnavailable):
        asyncio.run(assistant.answer("Вопрос"))
