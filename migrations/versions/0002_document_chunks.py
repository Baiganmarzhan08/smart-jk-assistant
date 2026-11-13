"""Create the document search index."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY


revision = "0002_document_chunks"
down_revision = "0001_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(200), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", ARRAY(sa.REAL()), nullable=True),
        sa.UniqueConstraint("document_id", "position", name="uq_document_chunks_position"),
    )


def downgrade() -> None:
    op.drop_table("document_chunks")
