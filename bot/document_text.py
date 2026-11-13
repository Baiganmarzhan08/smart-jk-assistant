from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import pymupdf
from docx import Document as WordDocument
from docx.text.paragraph import Paragraph
from lxml.etree import XMLSyntaxError


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_DOCX_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_DOCX_ENTRIES = 2000
MAX_PDF_PAGES = 200
MAX_TEXT_LENGTH = 200_000


class DocumentImportError(ValueError):
    """A document cannot be read or imported."""


@dataclass(frozen=True)
class ExtractedDocument:
    original_name: str
    file_format: str
    size_bytes: int
    sha256: str
    text: str


def extract_document(path: Path) -> ExtractedDocument:
    file_format = path.suffix.lower().removeprefix(".")
    if file_format not in {"pdf", "docx"}:
        raise DocumentImportError("Выберите файл PDF или DOCX.")
    if len(path.name) > 255:
        raise DocumentImportError("Имя файла должно содержать не более 255 символов.")

    try:
        with path.open("rb") as file:
            data = file.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise DocumentImportError("Не удалось прочитать файл. Проверьте путь и доступ.") from None
    if len(data) > MAX_FILE_BYTES:
        raise DocumentImportError("Размер файла превышает 10 МиБ.")
    if not data:
        raise DocumentImportError("Файл пуст.")

    text = extract_pdf(data) if file_format == "pdf" else extract_docx(data)
    if not text:
        raise DocumentImportError("Текст не найден. Для скана сначала выполните OCR и сохраните текстовый PDF.")
    if len(text) > MAX_TEXT_LENGTH:
        raise DocumentImportError("Текст превышает 200 000 символов. Разделите документ на части.")
    return ExtractedDocument(path.name, file_format, len(data), sha256(data).hexdigest(), text)


def extract_pdf(data: bytes) -> str:
    if not data.startswith(b"%PDF-"):
        raise DocumentImportError("Файл не является PDF.")
    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                raise DocumentImportError("Снимите пароль с PDF перед импортом.")
            if document.page_count > MAX_PDF_PAGES:
                raise DocumentImportError("PDF должен содержать не более 200 страниц.")
            pages = []
            length = 0
            for page in document:
                text = page.get_text(sort=True).replace("\0", "").strip()
                if text:
                    section = f"[Страница {page.number + 1}]\n{text}"
                    length += len(section) + 2
                    if length > MAX_TEXT_LENGTH:
                        raise DocumentImportError("Текст превышает 200 000 символов. Разделите документ на части.")
                    pages.append(section)
            return "\n\n".join(pages)
    except RuntimeError:
        raise DocumentImportError("Не удалось извлечь текст из PDF.") from None


def extract_docx(data: bytes) -> str:
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES or sum(entry.file_size for entry in entries) > MAX_DOCX_UNPACKED_BYTES:
                raise DocumentImportError("Содержимое DOCX превышает допустимый объём.")
            if "word/document.xml" not in archive.namelist():
                raise DocumentImportError("Файл не является DOCX.")
            if any(entry.flag_bits & 1 for entry in entries):
                raise DocumentImportError("Снимите пароль с DOCX перед импортом.")

        document = WordDocument(BytesIO(data))
        blocks = []
        for block in document.iter_inner_content():
            if isinstance(block, Paragraph):
                blocks.append(block.text)
            else:
                blocks.extend(" | ".join(cell.text.strip() for cell in row.cells) for row in block.rows)
    except DocumentImportError:
        raise
    except (BadZipFile, KeyError, RuntimeError, ValueError, XMLSyntaxError):
        raise DocumentImportError("Не удалось прочитать DOCX.") from None

    return "\n\n".join(text for block in blocks if (text := block.replace("\0", "").strip()))
