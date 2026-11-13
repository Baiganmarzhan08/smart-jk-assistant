import logging
import re
from asyncio import to_thread
from collections import Counter
from dataclasses import dataclass
from datetime import date
from math import log

from sqlalchemy import or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from bot.embeddings import EmbeddingUnavailable, GeminiEmbeddings
from bot.models import Document, DocumentChunk


LOGGER = logging.getLogger(__name__)
DOCUMENT_QUESTION = re.compile(
    r"\b(?:закон\w*|стать[яиюеё]\w*|статей|ст\.\s*\d+|кодекс\w*|норматив\w*|кондоминиум\w*|ОСИ|КСК|"
    r"права\s+(?:потребител\w*|собственник\w*|жител\w*|владельц\w*|нанимател\w*|арендатор\w*)|"
    r"персональн\w*\s+данн\w*|"
    r"общ\w*\s+имуществ\w*|собрани\w*\s+собственник\w*|коммунальн\w*\s+услуг\w*|"
    r"заң\w*|ба[пб]\w*|қағида\w*|құқығ\w*|құқық\w*|дербес\s+дерек\w*|ортақ\s+мүлік\w*|"
    r"коммуналдық\s+(?:қызмет\w*|көрсетілетін))\b",
    re.IGNORECASE,
)
ARTICLE = re.compile(
    r"\b(?:(?:стать[яиюеёй]|ст\.)\s*(\d+(?:-\d+)?)|(\d+(?:-\d+)?)\s*[-–]?\s*ба[пб]\w*)\b",
    re.IGNORECASE,
)
DEFINITION_QUESTION = re.compile(
    r"\b(?:не\s+жатады|не\s+кіреді|деген\s+не|что\s+такое|что\s+(?:относится|входит)|как\s+определяется)\b",
    re.IGNORECASE,
)
DEFINED_TERM = re.compile(r"^\s*\d+(?:-\d+)?\)\s+([^\n–—]{3,130}?)\s+[–—-]\s*", re.MULTILINE)
WORDS = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
STOP_WORDS = {
    "болады", "деген", "және", "туралы", "бойынша", "үшін", "қандай", "қалай", "қайсы", "жатады", "кіреді",
    "бойын", "тура", "есть", "какой", "какая", "какие", "можно", "этот", "закон", "статья",
    "где", "что", "как", "при", "для", "это", "когда", "указано", "относится", "входит", "такое", "определяется",
}
SEMANTIC_MIN = 0.48
RESULT_LIMIT = 6


@dataclass(frozen=True)
class Passage:
    id: int
    title: str
    section: str | None
    content: str
    source_url: str | None
    revision_date: date | None
    embedding: list[float] | None = None


def tokens(text: str) -> set[str]:
    return {word[:5] for word in WORDS.findall(text.casefold()) if word not in STOP_WORDS}


def article_numbers(text: str) -> set[str]:
    return {number for match in ARTICLE.finditer(text) for number in match.groups() if number}


def definition_score(content: str, question_terms: set[str]) -> float:
    best = 0.0
    for match in DEFINED_TERM.finditer(content):
        defined = tokens(match[1])
        overlap = len(defined & question_terms)
        if overlap and overlap >= min(2, len(question_terms)):
            best = max(best, 2 * overlap / (len(defined) + len(question_terms)))
    return best


def rank_passages(passages: list[Passage], question: str, query_vector: list[float] | None) -> list[Passage]:
    terms = tokens(question)
    requested_articles = article_numbers(question)
    wants_definition = bool(terms and DEFINITION_QUESTION.search(question))
    body_terms = [tokens(passage.content) for passage in passages]
    frequency = Counter(term for body in body_terms for term in body & terms)
    weights = {term: 1 + log((len(passages) + 1) / (frequency[term] + 1)) for term in terms}
    total_weight = sum(weights.values()) or 1
    ranked = []
    for passage, body in zip(passages, body_terms, strict=True):
        hits = terms & body
        lexical = sum(weights[term] for term in hits) / total_weight
        same_article = bool(requested_articles & article_numbers(passage.section or ""))
        semantic = 0.0
        if query_vector and passage.embedding and len(query_vector) == len(passage.embedding):
            semantic = sum(a * b for a, b in zip(query_vector, passage.embedding, strict=True))
        if requested_articles and not same_article:
            continue
        definition = definition_score(passage.content, terms) if wants_definition else 0.0
        if not (same_article or definition or semantic >= SEMANTIC_MIN or (hits and lexical >= 0.38)):
            continue
        title_match = len(terms & tokens(passage.title)) / max(1, len(terms))
        ranked.append((0.65 * semantic + 0.35 * lexical + 0.12 * title_match + 0.65 * definition, passage))
    ranked.sort(key=lambda result: (-result[0], result[1].id))
    return [passage for _, passage in ranked[:RESULT_LIMIT]]


def load_passages(engine: Engine, residence_id: int | None = None) -> list[Passage]:
    statement = select(
        DocumentChunk.id, Document.title, DocumentChunk.section, DocumentChunk.content,
        Document.source_url, Document.revision_date, DocumentChunk.embedding,
    ).join(Document).where(
        Document.is_public.is_(True), or_(Document.residence_id.is_(None), Document.residence_id == residence_id),
    )
    with Session(engine) as session:
        return [Passage(*row) for row in session.execute(statement)]


class KnowledgeBase:
    def __init__(self, engine: Engine, embeddings: GeminiEmbeddings) -> None:
        self.engine = engine
        self.embeddings = embeddings

    async def search(self, question: str, residence_id: int | None = None) -> list[Passage]:
        try:
            passages = await to_thread(load_passages, self.engine, residence_id)
        except SQLAlchemyError as error:
            LOGGER.warning("Document search: %s", type(error).__name__)
            return []
        requested_articles = article_numbers(question)
        if requested_articles:
            passages = [item for item in passages if requested_articles & article_numbers(item.section or "")]
        vector = None
        if any(passage.embedding for passage in passages):
            try:
                vector = await self.embeddings.query(question)
            except EmbeddingUnavailable:
                pass
        return await to_thread(rank_passages, passages, question, vector)
