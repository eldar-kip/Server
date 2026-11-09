"""add ml training jobs and model registry

Revision ID: d6f4a9b304e5
Revises: c5d3e8a203d4
Create Date: 2026-11-08 18:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d6f4a9b304e5"
down_revision: Union[str, Sequence[str], None] = "c5d3e8a203d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Создание таблицы ml_training_jobs для контура tAIres-Trainer и реестра моделей S3."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        training_status_enum = postgresql.ENUM(
            "PENDING",
            "TRAINING",
            "COMPLETED",
            "FAILED",
            name="trainingstatusenum",
            create_type=False,
        )
        training_status_enum.create(bind, checkfirst=True)
    else:
        training_status_enum = sa.Enum(
            "PENDING",
            "TRAINING",
            "COMPLETED",
            "FAILED",
            name="trainingstatusenum",
        )

    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

    op.create_table(
        "ml_training_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("model_version", sa.String(length=60), nullable=False),
        sa.Column("base_model", sa.String(length=255), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("epochs", sa.Integer(), nullable=False),
        sa.Column("learning_rate", sa.Float(), nullable=False),
        sa.Column("val_split", sa.Float(), nullable=False),
        sa.Column("dataset_size", sa.Integer(), nullable=False),
        sa.Column("status", training_status_enum, nullable=False),
        sa.Column("s3_uri", sa.String(length=500), nullable=True),
        sa.Column("autotest_accuracy", sa.Float(), nullable=True),
        sa.Column("telemetry", json_type, nullable=True),
        sa.Column("model_metadata", json_type, nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_ml_training_jobs_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ml_training_jobs")),
    )
    op.create_index(op.f("ix_ml_training_jobs_user_id"), "ml_training_jobs", ["user_id"], unique=False)
    op.create_index(op.f("ix_ml_training_jobs_model_name"), "ml_training_jobs", ["model_name"], unique=False)
    op.create_index(op.f("ix_ml_training_jobs_is_active"), "ml_training_jobs", ["is_active"], unique=False)


def downgrade() -> None:
    """Откат таблицы ml_training_jobs."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.drop_index(op.f("ix_ml_training_jobs_is_active"), table_name="ml_training_jobs")
    op.drop_index(op.f("ix_ml_training_jobs_model_name"), table_name="ml_training_jobs")
    op.drop_index(op.f("ix_ml_training_jobs_user_id"), table_name="ml_training_jobs")
    op.drop_table("ml_training_jobs")

    if is_pg:
        op.execute("DROP TYPE IF EXISTS trainingstatusenum")
