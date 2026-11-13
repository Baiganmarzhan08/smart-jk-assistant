import asyncio
import re

from aiohttp import ClientSession
from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from bot.config import load_gemini_settings
from bot.database import create_database_engine, database_url
from bot.embeddings import EmbeddingUnavailable, GeminiEmbeddings
from bot.models import Document, DocumentChunk


MAX_CHARS = 2200
BATCH_SIZE = 20
SECTION = re.compile(
    r"^(?:\[Страница \d+\]|(?:статья|глава|раздел)\s+\d+|\d+(?:-\d+)?\s*[-–]\s*(?:бап|тарау|бөлім)\b)",
    re.IGNORECASE,
)


def split_document(text: str) -> list[tuple[str | None, str]]:
    chunks = []
    section = None
    buffer = ""
    for paragraph in (part.strip() for part in text.splitlines()):
        if not paragraph:
            continue
        if SECTION.match(paragraph):
            if buffer:
                chunks.append((section, buffer))
            section = paragraph[:200]
            buffer = ""
        if buffer and len(buffer) + len(paragraph) + 2 > MAX_CHARS:
            chunks.append((section, buffer))
            buffer = ""
        while len(paragraph) > MAX_CHARS:
            boundary = paragraph.rfind(" ", 0, MAX_CHARS + 1)
            if boundary < MAX_CHARS // 2:
                boundary = MAX_CHARS
            chunks.append((section, paragraph[:boundary].rstrip()))
            paragraph = paragraph[boundary:].lstrip()
        buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
    if buffer:
        chunks.append((section, buffer))
    return chunks


def create_chunks(engine: Engine) -> int:
    query = select(Document.id, Document.extracted_text).where(
        Document.is_public.is_(True),
        ~exists().where(DocumentChunk.document_id == Document.id),
    )
    count = 0
    with Session(engine) as session, session.begin():
        for document_id, text in session.execute(query):
            rows = [
                {"document_id": document_id, "position": position, "section": section, "content": content}
                for position, (section, content) in enumerate(split_document(text), 1)
            ]
            if rows:
                statement = insert(DocumentChunk).values(rows).on_conflict_do_nothing(
                    constraint="uq_document_chunks_position"
                ).returning(DocumentChunk.id)
                count += len(session.scalars(statement).all())
    return count


def pending_chunks(engine: Engine) -> list[tuple[int, str, str]]:
    statement = (
        select(DocumentChunk.id, Document.title, DocumentChunk.content)
        .join(Document)
        .where(Document.is_public.is_(True), DocumentChunk.embedding.is_(None))
        .order_by(DocumentChunk.id)
        .limit(BATCH_SIZE)
    )
    with Session(engine) as session:
        return [tuple(row) for row in session.execute(statement)]


def save_embeddings(engine: Engine, ids: list[int], vectors: list[list[float]]) -> None:
    with Session(engine) as session, session.begin():
        session.execute(update(DocumentChunk), [
            {"id": chunk_id, "embedding": vector}
            for chunk_id, vector in zip(ids, vectors, strict=True)
        ])


def index_counts(engine: Engine) -> tuple[int, int]:
    statement = (
        select(func.count(DocumentChunk.id), func.count(DocumentChunk.embedding))
        .select_from(DocumentChunk)
        .join(Document)
        .where(Document.is_public.is_(True))
    )
    with Session(engine) as session:
        chunks, embedded = session.execute(statement).one()
        return chunks, embedded


async def fill_embeddings(engine: Engine, key: str) -> None:
    async with ClientSession() as session:
        client = GeminiEmbeddings(session, key)
        while batch := await asyncio.to_thread(pending_chunks, engine):
            values = [(title, content) for _, title, content in batch]
            try:
                vectors = await client.documents(values)
            except EmbeddingUnavailable as error:
                if error.status not in {429, 503}:
                    raise
                print(f"Gemini HTTP {error.status}. Повтор через 60 секунд.", flush=True)
                await asyncio.sleep(60)
                vectors = await client.documents(values)
            await asyncio.to_thread(save_embeddings, engine, [chunk_id for chunk_id, _, _ in batch], vectors)
            total, embedded = await asyncio.to_thread(index_counts, engine)
            print(f"Векторы: {embedded}/{total}", flush=True)


def main() -> None:
    try:
        engine = create_database_engine(database_url())
        try:
            print(f"Новых фрагментов: {create_chunks(engine)}", flush=True)
            total, embedded = index_counts(engine)
            print(f"Векторы: {embedded}/{total}", flush=True)
            key, _ = load_gemini_settings()
            if total != embedded and not key:
                raise SystemExit("Для семантического индекса укажите GEMINI_API_KEY и повторите команду.")
            if total != embedded:
                asyncio.run(fill_embeddings(engine, key))
        finally:
            engine.dispose()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except SQLAlchemyError:
        raise SystemExit("Ошибка PostgreSQL. Проверьте DATABASE_URL и миграцию Alembic.") from None
    except EmbeddingUnavailable as error:
        raise SystemExit(f"{error}. Готовые векторы сохранены. Повторите команду позже.") from None


if __name__ == "__main__":
    main()
