"""add matching fields and feedback audit log

Revision ID: a3f1c8d901b2
Revises: 2afb21c305de
Create Date: 2026-11-08 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a3f1c8d901b2"
down_revision: Union[str, Sequence[str], None] = "2afb21c305de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Добавление владельца сессии, полей матчинга, векторов и таблицы аудита."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("ALTER TYPE taskstatusenum ADD VALUE IF NOT EXISTS 'APPROVED'")

    if is_pg:
        match_status_enum = postgresql.ENUM(
            "AUTO",
            "NEEDS_REVIEW",
            "MANUAL",
            "UNMATCHED",
            name="matchstatusenum",
            create_type=False,
        )
        audit_action_enum = postgresql.ENUM(
            "MANUAL_REMATCH",
            "APPROVE",
            name="auditactionenum",
            create_type=False,
        )
        match_status_enum.create(bind, checkfirst=True)
        audit_action_enum.create(bind, checkfirst=True)
    else:
        match_status_enum = sa.Enum(
            "AUTO",
            "NEEDS_REVIEW",
            "MANUAL",
            "UNMATCHED",
            name="matchstatusenum",
        )
        audit_action_enum = sa.Enum(
            "MANUAL_REMATCH",
            "APPROVE",
            name="auditactionenum",
        )

    # 1. Владелец закупочной сессии
    with op.batch_alter_table("procurement_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_procurement_sessions_user_id"),
            ["user_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_procurement_sessions_user_id_users"),
            "users",
            ["user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 2. Поля векторизации каталога шин
    embedding_type = Vector(1024) if is_pg else sa.JSON()
    with op.batch_alter_table("tires", schema=None) as batch_op:
        batch_op.add_column(sa.Column("search_text", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("embedding", embedding_type, nullable=True))

    # 3. Поля результата сопоставления у позиций прайс-листа
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    with op.batch_alter_table("raw_offer_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("matched_tire_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("confidence_score", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "match_status",
                match_status_enum,
                server_default="UNMATCHED",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("candidates", json_type, nullable=True))
        batch_op.add_column(sa.Column("embedding", embedding_type, nullable=True))
        batch_op.create_index(
            batch_op.f("ix_raw_offer_items_session_id"),
            ["session_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_raw_offer_items_confidence_score"),
            ["confidence_score"],
            unique=False,
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_raw_offer_items_matched_tire_id_tires"),
            "tires",
            ["matched_tire_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 4. Таблица логирования обратной связи закупщика (FeedbackAuditLog)
    op.create_table(
        "feedback_audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", audit_action_enum, nullable=False),
        sa.Column("old_tire_id", sa.Integer(), nullable=True),
        sa.Column("new_tire_id", sa.Integer(), nullable=True),
        sa.Column("old_confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["procurement_sessions.id"],
            name=op.f("fk_feedback_audit_logs_session_id_procurement_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["raw_offer_items.id"],
            name=op.f("fk_feedback_audit_logs_item_id_raw_offer_items"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_feedback_audit_logs_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["old_tire_id"],
            ["tires.id"],
            name=op.f("fk_feedback_audit_logs_old_tire_id_tires"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["new_tire_id"],
            ["tires.id"],
            name=op.f("fk_feedback_audit_logs_new_tire_id_tires"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback_audit_logs")),
    )
    op.create_index(
        op.f("ix_feedback_audit_logs_session_id"),
        "feedback_audit_logs",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    """Откат полей матчинга, векторов и таблицы аудита."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.drop_index(op.f("ix_feedback_audit_logs_session_id"), table_name="feedback_audit_logs")
    op.drop_table("feedback_audit_logs")

    with op.batch_alter_table("raw_offer_items", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_raw_offer_items_matched_tire_id_tires"),
            type_="foreignkey",
        )
        batch_op.drop_index(batch_op.f("ix_raw_offer_items_confidence_score"))
        batch_op.drop_index(batch_op.f("ix_raw_offer_items_session_id"))
        batch_op.drop_column("embedding")
        batch_op.drop_column("candidates")
        batch_op.drop_column("match_status")
        batch_op.drop_column("confidence_score")
        batch_op.drop_column("matched_tire_id")

    with op.batch_alter_table("tires", schema=None) as batch_op:
        batch_op.drop_column("embedding")
        batch_op.drop_column("search_text")

    with op.batch_alter_table("procurement_sessions", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_procurement_sessions_user_id_users"),
            type_="foreignkey",
        )
        batch_op.drop_index(batch_op.f("ix_procurement_sessions_user_id"))
        batch_op.drop_column("user_id")

    if is_pg:
        op.execute("DROP TYPE IF EXISTS auditactionenum")
        op.execute("DROP TYPE IF EXISTS matchstatusenum")
        op.execute(
            "UPDATE procurement_sessions SET status = 'COMPLETED' WHERE status::text = 'APPROVED'"
        )
        op.execute(
            "CREATE TYPE taskstatusenum_old AS ENUM ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')"
        )
        op.execute(
            "ALTER TABLE procurement_sessions "
            "ALTER COLUMN status TYPE taskstatusenum_old USING status::text::taskstatusenum_old"
        )
        op.execute("DROP TYPE taskstatusenum")
        op.execute("ALTER TYPE taskstatusenum_old RENAME TO taskstatusenum")
