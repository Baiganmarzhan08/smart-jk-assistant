import argparse
from datetime import date
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from bot.database import create_database_engine, database_url
from bot.document_store import import_document, list_documents
from bot.residents import get_residence


def main() -> None:
    parser = argparse.ArgumentParser(description="Импорт и просмотр документов Smart ЖК.")
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="Извлечь текст из PDF/DOCX и сохранить в базе.")
    importer.add_argument("file", type=Path)
    importer.add_argument("--title", required=True)
    importer.add_argument("--source-url")
    importer.add_argument("--revision-date", type=date.fromisoformat, metavar="ГГГГ-ММ-ДД")
    importer.add_argument("--public", action="store_true", help="Показать название документа жителям.")
    importer.add_argument("--residence", help="Код ЖК для локального документа. Общие законы загружайте без этого флага.")
    commands.add_parser("list", help="Показать последние 50 документов.")
    args = parser.parse_args()

    try:
        engine = create_database_engine(database_url())
        try:
            if args.command == "import":
                residence_id = None
                if args.residence:
                    with Session(engine) as session:
                        residence_id = get_residence(session, args.residence).id
                document_id, created = import_document(
                    engine,
                    args.file,
                    title=args.title,
                    source_url=args.source_url,
                    revision_date=args.revision_date,
                    public=args.public,
                    residence_id=residence_id,
                )
                status = "Импортирован" if created else "Уже в базе"
                visibility = " (доступен в /documents)" if args.public else ""
                print(f"{status} документ №{document_id}{visibility}.")
            else:
                documents = list_documents(engine, public_only=False, limit=50)
                if not documents:
                    print("Документов пока нет.")
                for document in documents:
                    visibility = "публичный" if document.is_public else "внутренний"
                    revision = document.revision_date or "редакция не указана"
                    source = document.source_url or "источник не указан"
                    print(f"{document.id}. {document.title} ({document.file_format.upper()}, {visibility})")
                    print(f"   {revision} | {source}")
        finally:
            engine.dispose()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except SQLAlchemyError:
        raise SystemExit("Ошибка PostgreSQL. Проверьте DATABASE_URL и выполните миграцию Alembic.") from None


if __name__ == "__main__":
    main()
