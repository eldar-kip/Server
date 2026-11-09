"""add hnsw index for tire embeddings

Revision ID: b4e2d7f102c3
Revises: a3f1c8d901b2
Create Date: 2026-11-08 17:05:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "b4e2d7f102c3"
down_revision: Union[str, Sequence[str], None] = "a3f1c8d901b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Создание индекса HNSW по косинусному расстоянию для ускорения k-NN поиска."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_tires_embedding_hnsw "
            "ON tires USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )


def downgrade() -> None:
    """Удаление индекса HNSW."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_tires_embedding_hnsw")
