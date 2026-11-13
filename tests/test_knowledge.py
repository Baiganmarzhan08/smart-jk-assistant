import asyncio
import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.assistant import GeminiAssistant, format_legal_answer, unconfirmed_answer
from bot.embeddings import DIMENSIONS, EmbeddingUnavailable, GeminiEmbeddings
from bot.knowledge import KnowledgeBase, Passage, rank_passages
from bot.knowledge_index import MAX_CHARS, split_document


URL = "https://old.adilet.zan.kz/kaz/docs/Z970000094_"
QUOTE = "Ортақ мүлікке баспалдақтар мен лифтілер жатады."
PASSAGE = Passage(
    1, "Тұрғын үй қатынастары туралы", "2-бап. Ортақ мүлік", QUOTE, URL, date(2026, 9, 14),
)


def test_splitting_keeps_articles_and_size_limit():
    first = "1-бап. Атауы"
    second = "2-бап. Екінші бөлім"
    chunks = split_document(first + "\n\n" + ("мәтін " * 1000).strip() + "\n\n" + second + "\nҚорытынды.")
    assert len(chunks) > 3
    assert all(len(content) <= MAX_CHARS for _, content in chunks)
    assert all(section == first for section, _ in chunks[:-1])
    assert chunks[-1] == (second, second + "\n\nҚорытынды.")


def test_lexical_fallback_and_exact_article():
    other = Passage(2, "Басқа құжат", "7-бап. Кіріспе", "Кондоминиумның ортақ мүлкі.", None, None)
    assert rank_passages([other, PASSAGE], "2-бабында не жазылған?", None) == [PASSAGE]
    assert rank_passages([PASSAGE], "999-бап не дейді?", None) == []
    assert rank_passages([PASSAGE], "Ортақ мүлікке лифтілер жата ма?", None) == [PASSAGE]
    assert rank_passages([PASSAGE], "Астрономиялық телескоп", None) == []


def test_semantic_search_matches_cross_language():
    semantic = Passage(2, PASSAGE.title, PASSAGE.section, PASSAGE.content, URL, None, [1.0, 0.0])
    irrelevant = Passage(3, "Басқа құжат", None, "Кесте", None, None, [0.0, 1.0])
    assert rank_passages([irrelevant, semantic], "Какие объекты считаются общим имуществом?", [1.0, 0.0]) == [semantic]


def test_definition_ranks_before_related_registration_article():
    definition = Passage(
        2, PASSAGE.title, "2-бап. Негізгі ұғымдар",
        "14) кондоминиум объектісінің ортақ мүлкі – кіреберістер, лифтілер және шатырлар.",
        URL, None, [0.65, 0.0],
    )
    registration = Passage(
        1, PASSAGE.title, "31-бап. Тіркеу", "Кондоминиумның ортақ мүлкі тіркеледі.", URL, None, [1.0, 0.0],
    )
    found = rank_passages([registration, definition], "Кондоминиумның ортақ мүлкіне не жатады?", [1.0, 0.0])
    assert found[0] == definition


def test_search_survives_embedding_error(monkeypatch):
    monkeypatch.setattr("bot.knowledge.load_passages", lambda engine, residence_id: [
        Passage(1, PASSAGE.title, PASSAGE.section, PASSAGE.content, URL, None, [1.0]),
    ])
    embeddings = MagicMock()
    embeddings.query = AsyncMock(side_effect=EmbeddingUnavailable(429))
    found = asyncio.run(KnowledgeBase(object(), embeddings).search("Ортақ мүлікке лифтілер жата ма?"))
    assert [item.id for item in found] == [1]


def test_missing_article_skips_external_requests(monkeypatch):
    monkeypatch.setattr("bot.knowledge.load_passages", lambda engine, residence_id: [PASSAGE])
    embeddings = MagicMock()
    embeddings.query = AsyncMock()
    knowledge = KnowledgeBase(object(), embeddings)
    provider = MagicMock()
    assistant = GeminiAssistant(provider, "mock-key", "mock-model", knowledge)
    question = "9999-бап не дейді?"
    assert asyncio.run(assistant.answer(question)) == unconfirmed_answer(question)
    embeddings.query.assert_not_awaited()
    provider.post.assert_not_called()


def test_embedding_payload_and_normalization():
    async def scenario():
        session = MagicMock()
        response = session.post.return_value.__aenter__.return_value
        response.json.return_value = {"embeddings": [{"values": [3.0, 4.0] + [0.0] * (DIMENSIONS - 2)}]}
        client = GeminiEmbeddings(session, "mock-key")
        vectors = await client.documents([("ЖК", "Мәтін")])
        assert vectors[0][:2] == [0.6, 0.8]
        batch = session.post.call_args.kwargs["json"]["requests"]
        assert batch[0]["taskType"] == "RETRIEVAL_DOCUMENT"
        assert batch[0]["outputDimensionality"] == DIMENSIONS
        response.json.return_value = {"embedding": {"values": [0.0] * DIMENSIONS}}
        with pytest.raises(EmbeddingUnavailable):
            await client.query("Сұрақ")
        assert session.post.call_args.kwargs["json"]["taskType"] == "RETRIEVAL_QUERY"

    asyncio.run(scenario())


def test_assistant_uses_only_verified_passages_for_legal_question():
    async def scenario():
        session = MagicMock()
        knowledge = MagicMock()
        knowledge.search = AsyncMock(return_value=[PASSAGE])
        assistant = GeminiAssistant(session, "mock-key", "mock-model", knowledge)
        assistant._generate_text = AsyncMock(return_value=json.dumps({
            "answer": "Иә, лифтілер ортақ мүлікке жатады.",
            "evidence": [{"source": 1, "quote": QUOTE}],
        }))
        question = "Лифт ортақ мүлікке жата ма?"
        answer = await assistant.answer(question)
        assert "Дереккөздер" in answer and URL in answer
        assert "2-бап" in answer and "14.09.2026" in answer
        knowledge.search.assert_awaited_once_with(question)
        payload = assistant._generate_text.call_args.args[0]
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        assert json.loads(payload["contents"][0]["parts"][0]["text"])["sources"][0]["content"] == QUOTE

    asyncio.run(scenario())


@pytest.mark.parametrize(("answer", "source", "quote"), [
    ("Да.", 7, QUOTE),
    ("Да.", 1, "Содержание придумано моделью."),
    ("По статье 999 это разрешено.", 1, QUOTE),
    ("См. https://example.org", 1, QUOTE),
])
def test_rejects_fabricated_evidence(answer, source, quote):
    question = "Что такое общее имущество?"
    raw = json.dumps({"answer": answer, "evidence": [{"source": source, "quote": quote}]})
    assert format_legal_answer(question, raw, [PASSAGE]) == unconfirmed_answer(question)
