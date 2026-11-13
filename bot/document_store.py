from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from bot.document_text import DocumentImportError, extract_document
from bot.models import Document


@dataclass(frozen=True)
class DocumentSummary:
    id: int
    title: str
    file_format: str
    revision_date: date | None
    source_url: str | None
    is_public: bool


def import_document(
    engine: Engine,
    path: Path,
    *,
    title: str,
    source_url: str | None = None,
    revision_date: date | None = None,
    public: bool = False,
    residence_id: int | None = None,
) -> tuple[int, bool]:
    title = " ".join(title.split())
    if not 1 <= len(title) <= 200:
        raise DocumentImportError("Название должно содержать от 1 до 200 символов.")
    if source_url:
        try:
            parsed = urlsplit(source_url)
            valid = (
                len(source_url) <= 1000
                and parsed.scheme in {"https", "http"}
                and parsed.hostname
                and parsed.username is None
                and not any(character.isspace() for character in source_url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise DocumentImportError("Источник должен быть ссылкой HTTP(S) длиной до 1000 символов.")

    data = extract_document(path)
    statement = (
        insert(Document)
        .values(
            title=title,
            source_url=source_url or None,
            revision_date=revision_date,
            original_name=data.original_name,
            file_format=data.file_format,
            size_bytes=data.size_bytes,
            sha256=data.sha256,
            extracted_text=data.text,
            is_public=public,
            residence_id=residence_id,
        )
        .on_conflict_do_nothing(constraint="uq_documents_scope_sha256")
        .returning(Document.id)
    )
    with Session(engine) as session, session.begin():
        inserted_id = session.scalar(statement)
        if inserted_id is not None:
            return inserted_id, True
        existing_id = session.scalars(select(Document.id).where(
            Document.sha256 == data.sha256, Document.residence_id == residence_id,
        )).one()
        if public:
            session.execute(update(Document).where(Document.id == existing_id).values(is_public=True))
        return existing_id, False


def list_documents(
    engine: Engine, *, public_only: bool = True, limit: int = 10, residence_id: int | None = None,
) -> list[DocumentSummary]:
    statement = select(
        Document.id, Document.title, Document.file_format, Document.revision_date, Document.source_url, Document.is_public
    ).order_by(Document.id.desc()).limit(limit)
    if public_only:
        statement = statement.where(
            Document.is_public.is_(True), or_(Document.residence_id.is_(None), Document.residence_id == residence_id),
        )
    with Session(engine) as session:
        return [DocumentSummary(*row) for row in session.execute(statement)]
