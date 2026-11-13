"""Add residences, buildings, resident tickets and service catalogs."""

from alembic import op
import sqlalchemy as sa


revision = "0003_residences"
down_revision = "0002_document_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    residences = op.create_table(
        "residences",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.String(48), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("address", sa.String(240)),
        sa.Column("dispatcher_contact", sa.String(160)),
        sa.Column("telegram_chat_id", sa.BigInteger, unique=True),
        sa.Column("is_demo", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    buildings = op.create_table(
        "buildings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("residence_id", sa.Integer, sa.ForeignKey("residences.id"), nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("entrances", sa.Integer),
        sa.Column("apartments", sa.Integer),
        sa.UniqueConstraint("residence_id", "label", name="uq_buildings_residence_label"),
        sa.UniqueConstraint("residence_id", "id", name="uq_buildings_residence_id"),
        sa.CheckConstraint("entrances > 0 AND apartments > 0", name="ck_buildings_counts"),
    )
    op.create_table(
        "users",
        sa.Column("telegram_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("residence_id", sa.Integer, sa.ForeignKey("residences.id")),
    )
    services = op.create_table(
        "services",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("residence_id", sa.Integer, sa.ForeignKey("residences.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("price", sa.String(80)),
        sa.Column("contact", sa.String(120)),
        sa.Column("available", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("residence_id", "name", name="uq_services_residence_name"),
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("submission_key", sa.Uuid, nullable=False, unique=True),
        sa.Column("telegram_id", sa.BigInteger, sa.ForeignKey("users.telegram_id"), nullable=False),
        sa.Column("residence_id", sa.Integer, sa.ForeignKey("residences.id"), nullable=False),
        sa.Column("building_id", sa.Integer, nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("entrance", sa.Integer, nullable=False),
        sa.Column("apartment", sa.String(20)),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["residence_id", "building_id"], ["buildings.residence_id", "buildings.id"],
            name="fk_tickets_building_residence",
        ),
        sa.CheckConstraint("entrance > 0", name="ck_tickets_entrance"),
        sa.CheckConstraint("status IN ('new', 'in_progress', 'done', 'rejected')", name="ck_tickets_status"),
        sa.CheckConstraint(
            "category IN ('lift','electricity','water','heating','cleaning','parking','lighting','yard','other')",
            name="ck_tickets_category",
        ),
    )
    op.create_index("ix_tickets_telegram_id", "tickets", ["telegram_id"])
    op.add_column("documents", sa.Column("residence_id", sa.Integer))
    op.create_foreign_key("fk_documents_residence", "documents", "residences", ["residence_id"], ["id"])
    op.drop_constraint("documents_sha256_key", "documents", type_="unique")
    op.create_unique_constraint(
        "uq_documents_scope_sha256", "documents", ["residence_id", "sha256"], postgresql_nulls_not_distinct=True,
    )

    residence_id = sa.select(residences.c.id).where(residences.c.slug == "smart-residence").scalar_subquery()
    op.execute(residences.insert().values(
        slug="smart-residence", name="Smart Residence", address="Учебный адрес: ул. Примерная, 10", is_demo=True,
    ))
    for label, entrances, apartments in (("А", 3, 96), ("Б", 2, 64)):
        op.execute(buildings.insert().values(
            residence_id=residence_id, label=label, entrances=entrances, apartments=apartments,
        ))
    for name, category, description in (
        ("Сантехник", "water", "Помощь с протечками и сантехникой."),
        ("Электрик", "electricity", "Проверка освещения и электрики."),
        ("Клининг", "cleaning", "Уборка общих зон."),
        ("Мелкий ремонт", "other", "Мелкие работы по дому."),
        ("Обслуживание оборудования", "other", "Диагностика и обслуживание."),
    ):
        op.execute(services.insert().values(
            residence_id=residence_id, name=name, category=category, description=description,
        ))


def downgrade() -> None:
    op.drop_constraint("uq_documents_scope_sha256", "documents", type_="unique")
    op.create_unique_constraint("documents_sha256_key", "documents", ["sha256"])
    op.drop_constraint("fk_documents_residence", "documents", type_="foreignkey")
    op.drop_column("documents", "residence_id")
    op.drop_table("tickets")
    op.drop_table("services")
    op.drop_table("users")
    op.drop_table("buildings")
    op.drop_table("residences")
