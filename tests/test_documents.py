from concurrent.futures import ThreadPoolExecutor
from datetime import date
from os import getenv
from uuid import uuid4

import pymupdf
import pytest
from alembic import command
from alembic.config import Config
from docx import Document as WordDocument
from sqlalchemy import delete, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from bot import document_text
from bot.config import PROJECT_DIR
from bot.database import create_database_engine
from bot.document_store import import_document, list_documents
from bot.document_text import DocumentImportError, extract_document
from bot.knowledge import load_passages
from bot.knowledge_index import create_chunks, index_counts, pending_chunks, save_embeddings
from bot.models import Building, Document, Residence, Service
from bot.residents import (
    ResidentInputError, change_ticket_status, create_ticket, own_tickets, residence_profile, select_residence,
)


@pytest.fixture
def pdf_path(tmp_path):
    path = tmp_path / "demo.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((40, 60), "Smart Residence rules")
        document.new_page().insert_text((40, 60), "Shared areas")
        document.save(path)
    return path


@pytest.fixture
def docx_path(tmp_path):
    path = tmp_path / "демо.docx"
    document = WordDocument()
    document.add_heading("Правила Smart Residence")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Ортақ аумақ"
    table.cell(0, 1).text = "Тазалық"
    document.add_paragraph("Общие зоны")
    document.save(path)
    return path


def test_extract_pdf_keeps_page_numbers(pdf_path):
    document = extract_document(pdf_path)
    assert document.text == "[Страница 1]\nSmart Residence rules\n\n[Страница 2]\nShared areas"
    assert document.file_format == "pdf"
    assert document.size_bytes == pdf_path.stat().st_size
    assert len(document.sha256) == 64


def test_extract_docx_keeps_heading_and_table_order(docx_path):
    document = extract_document(docx_path)
    assert document.text == "Правила Smart Residence\n\nОртақ аумақ | Тазалық\n\nОбщие зоны"
    assert document.original_name == "демо.docx"


@pytest.mark.parametrize(("name", "body", "error"), [
    ("demo.pdf", b"not a pdf", "не является PDF"),
    ("broken.pdf", b"%PDF-1.7\ncorrupt", "Не удалось извлечь"),
    ("demo.docx", b"not a zip", "Не удалось прочитать"),
    ("demo.txt", b"plain text", "PDF или DOCX"),
    ("empty.pdf", b"", "пуст"),
])
def test_bad_document_rejected(tmp_path, name, body, error):
    path = tmp_path / name
    path.write_bytes(body)
    with pytest.raises(DocumentImportError, match=error):
        extract_document(path)


def test_limits_and_pdf_without_text(tmp_path, pdf_path, docx_path, monkeypatch):
    path = tmp_path / "scan.pdf"
    with pymupdf.open() as document:
        document.new_page()
        document.save(path)
    with pytest.raises(DocumentImportError, match="Текст не найден"):
        extract_document(path)

    monkeypatch.setattr(document_text, "MAX_PDF_PAGES", 1)
    with pytest.raises(DocumentImportError, match="страниц"):
        extract_document(pdf_path)
    monkeypatch.setattr(document_text, "MAX_DOCX_UNPACKED_BYTES", 1)
    with pytest.raises(DocumentImportError, match="объём"):
        extract_document(docx_path)
    monkeypatch.setattr(document_text, "MAX_FILE_BYTES", 1)
    with pytest.raises(DocumentImportError, match="Размер"):
        extract_document(pdf_path)


def test_document_metadata_validated_before_storage(pdf_path):
    with pytest.raises(DocumentImportError, match="Название"):
        import_document(None, pdf_path, title="   ")
    with pytest.raises(DocumentImportError, match="HTTP"):
        import_document(None, pdf_path, title="Правила", source_url="javascript:alert(1)")


def test_postgres_migration_and_document_import(monkeypatch, pdf_path, docx_path):
    test_url = getenv("SMART_JK_TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("SMART_JK_TEST_DATABASE_URL is required for PostgreSQL integration")

    schema = f"smart_jk_test_{uuid4().hex}"
    url = make_url(test_url).set(drivername="postgresql+psycopg")
    setup = create_database_engine(url)
    with setup.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    monkeypatch.setenv("DATABASE_URL", scoped_url.render_as_string(hide_password=False))
    engine = create_database_engine(scoped_url)
    migration = Config(str(PROJECT_DIR / "alembic.ini"))
    try:
        command.upgrade(migration, "head")
        command.check(migration)
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(import_document, engine, pdf_path, title="Правила ЖК") for _ in range(2)]
            results = [task.result(timeout=10) for task in tasks]
        document_id = results[0][0]
        assert {result[0] for result in results} == {document_id}
        assert sorted(result[1] for result in results) == [False, True]
        assert list_documents(engine) == []
        assert create_chunks(engine) == 0

        assert import_document(engine, pdf_path, title="Повторный импорт", public=True) == (document_id, False)
        source = "https://example.com/demo"
        revision = date(2026, 11, 13)
        other_id, created = import_document(
            engine, docx_path, title="Контакты ЖК", source_url=source, revision_date=revision, public=True
        )
        assert created
        latest, first = list_documents(engine)
        assert (latest.id, latest.source_url, latest.revision_date) == (other_id, source, revision)
        assert (first.id, first.title) == (document_id, "Правила ЖК")
        with Session(engine) as session:
            texts = list(session.scalars(select(Document.extracted_text).order_by(Document.id)))
        assert "Shared areas" in texts[0]
        assert "Ортақ аумақ" in texts[1]

        assert create_chunks(engine) == 3
        assert create_chunks(engine) == 0
        assert index_counts(engine) == (3, 0)
        chunks = pending_chunks(engine)
        save_embeddings(engine, [chunks[0][0]], [[1.0] + [0.0] * 767])
        assert index_counts(engine) == (3, 1)
        assert len(load_passages(engine)) == 3

        demo = residence_profile(engine)
        assert demo.residence.is_demo and len(demo.services) == 5
        assert "Квартир всего: 160" in demo.facts()
        with Session(engine) as session, session.begin():
            residence = Residence(slug="qa", name="QA ЖК", telegram_chat_id=-100123)
            session.add(residence)
            session.flush()
            building = Building(residence_id=residence.id, label="1", entrances=2, apartments=40)
            session.add(building)
            session.add(Service(
                residence_id=residence.id, name="QA услуга", category="other", description="Описание", available=False,
            ))
            session.flush()
            residence_id, building_id = residence.id, building.id
        assert not residence_profile(engine, group_chat_id=-100123).services
        select_residence(engine, 7_000_000_001, "qa")
        assert residence_profile(engine, telegram_id=7_000_000_001).residence.id == residence_id
        parameters = dict(
            submission_key=uuid4(), telegram_id=7_000_000_001, residence_id=residence_id,
            building_id=building_id, category="lighting", entrance=2, description="Тестовая заявка",
        )
        with pytest.raises(ResidentInputError):
            create_ticket(engine, **{**parameters, "entrance": 3})
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(create_ticket, engine, **parameters) for _ in range(2)]
            ticket_ids = {task.result(timeout=10) for task in tasks}
        assert len(ticket_ids) == 1
        ticket_id = ticket_ids.pop()
        assert own_tickets(engine, 7_000_000_002, ticket_id) == []
        assert own_tickets(engine, 7_000_000_001, ticket_id)[0].status == "new"
        assert change_ticket_status(engine, ticket_id, "in_progress")
        assert own_tickets(engine, 7_000_000_001, ticket_id)[0].status == "in_progress"

        scoped_id, _ = import_document(engine, pdf_path, title="Только QA", public=True, residence_id=residence_id)
        assert all(item.id != scoped_id for item in list_documents(engine))
        assert any(item.id == scoped_id for item in list_documents(engine, residence_id=residence_id))
        assert create_chunks(engine) == 2
        assert len(load_passages(engine)) == 3
        assert len(load_passages(engine, residence_id)) == 5
        with Session(engine) as session, session.begin():
            session.execute(delete(Document).where(Document.id == scoped_id))

        command.downgrade(migration, "base")
        assert not inspect(engine).has_table("documents")
        command.upgrade(migration, "head")
        assert list_documents(engine) == []
    finally:
        engine.dispose()
        with setup.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        setup.dispose()
