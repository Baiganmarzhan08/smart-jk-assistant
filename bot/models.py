from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint,
    Integer, REAL, String, Text, UniqueConstraint, Uuid, false, func, true,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from uuid import UUID


class Base(DeclarativeBase):
    pass


class Residence(Base):
    __tablename__ = "residences"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(48), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(String(240))
    dispatcher_contact: Mapped[str | None] = mapped_column(String(160))
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, server_default=false())


class Building(Base):
    __tablename__ = "buildings"
    __table_args__ = (
        UniqueConstraint("residence_id", "label", name="uq_buildings_residence_label"),
        UniqueConstraint("residence_id", "id", name="uq_buildings_residence_id"),
        CheckConstraint("entrances > 0 AND apartments > 0", name="ck_buildings_counts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    residence_id: Mapped[int] = mapped_column(ForeignKey("residences.id"))
    label: Mapped[str] = mapped_column(String(60))
    entrances: Mapped[int | None] = mapped_column(Integer)
    apartments: Mapped[int | None] = mapped_column(Integer)


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    residence_id: Mapped[int | None] = mapped_column(ForeignKey("residences.id"))


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (UniqueConstraint("residence_id", "name", name="uq_services_residence_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    residence_id: Mapped[int] = mapped_column(ForeignKey("residences.id"))
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(String(300))
    price: Mapped[str | None] = mapped_column(String(80))
    contact: Mapped[str | None] = mapped_column(String(120))
    available: Mapped[bool] = mapped_column(Boolean, server_default=true())


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["residence_id", "building_id"], ["buildings.residence_id", "buildings.id"],
            name="fk_tickets_building_residence",
        ),
        CheckConstraint("entrance > 0", name="ck_tickets_entrance"),
        CheckConstraint(
            "status IN ('new', 'in_progress', 'done', 'rejected')", name="ck_tickets_status",
        ),
        CheckConstraint(
            "category IN ('lift','electricity','water','heating','cleaning','parking','lighting','yard','other')",
            name="ck_tickets_category",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_key: Mapped[UUID] = mapped_column(Uuid, unique=True)
    telegram_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    residence_id: Mapped[int] = mapped_column(ForeignKey("residences.id"))
    building_id: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(String(1000))
    entrance: Mapped[int] = mapped_column(Integer)
    apartment: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), server_default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("file_format IN ('pdf', 'docx')", name="ck_documents_file_format"),
        CheckConstraint("size_bytes > 0", name="ck_documents_size_bytes"),
        UniqueConstraint("residence_id", "sha256", name="uq_documents_scope_sha256", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    revision_date: Mapped[date | None] = mapped_column(Date)
    original_name: Mapped[str] = mapped_column(String(255))
    file_format: Mapped[str] = mapped_column(String(4))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    extracted_text: Mapped[str] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean, server_default=false())
    residence_id: Mapped[int | None] = mapped_column(ForeignKey("residences.id", name="fk_documents_residence"))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "position", name="uq_document_chunks_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(REAL))
