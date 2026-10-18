"""init pgvector extension

Revision ID: 2afb21c305de
Revises: 81359e44c8e4
Create Date: 2026-09-30 23:03:46.982083

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2afb21c305de'
down_revision: Union[str, Sequence[str], None] = '81359e44c8e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Инициализация расширения
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector;")
