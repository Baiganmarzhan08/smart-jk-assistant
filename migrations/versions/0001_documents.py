"""Create the document catalog."""

from alembic import op
import sqlalchemy as sa


revision = "0001_documents"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("source_url", sa.String(1000), nullable=True),
        sa.Column("revision_date", sa.Date(), nullable=True),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("file_format", sa.String(4), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("extracted_text", sa.Text(), nullable=False),
        sa.Column("is_public", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("file_format IN ('pdf', 'docx')", name="ck_documents_file_format"),
        sa.CheckConstraint("size_bytes > 0", name="ck_documents_size_bytes"),
    )


def downgrade() -> None:
    op.drop_table("documents")
