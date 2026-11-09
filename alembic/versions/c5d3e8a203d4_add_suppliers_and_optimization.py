"""add suppliers demand items and optimization results

Revision ID: c5d3e8a203d4
Revises: b4e2d7f102c3
Create Date: 2026-11-08 17:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c5d3e8a203d4"
down_revision: Union[str, Sequence[str], None] = "b4e2d7f102c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Добавление поставщиков, квантов поставки, потребности и результатов SplitOptimizer."""
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

    # 1. Справочник поставщиков
    op.create_table(
        "suppliers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("inn", sa.String(length=20), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suppliers")),
        sa.UniqueConstraint("inn", name=op.f("uq_suppliers_inn")),
    )

    # 2. Поля поставщика и кратности поставки в строках прайс-листа
    with op.batch_alter_table("raw_offer_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("supplier_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("lot_size", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.add_column(
            sa.Column("min_order_qty", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.create_index(
            batch_op.f("ix_raw_offer_items_supplier_id"),
            ["supplier_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_raw_offer_items_supplier_id_suppliers"),
            "suppliers",
            ["supplier_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 3. Таблица потребности закупщика (DemandItem)
    op.create_table(
        "demand_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("tire_id", sa.Integer(), nullable=False),
        sa.Column("required_qty", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["procurement_sessions.id"],
            name=op.f("fk_demand_items_session_id_procurement_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tire_id"],
            ["tires.id"],
            name=op.f("fk_demand_items_tire_id_tires"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demand_items")),
    )
    op.create_index(
        op.f("ix_demand_items_session_id"),
        "demand_items",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_demand_items_tire_id"),
        "demand_items",
        ["tire_id"],
        unique=False,
    )

    # 4. Таблица результатов оптимизации сплита заказов (OptimizationResult)
    op.create_table(
        "optimization_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("total_cost", sa.Float(), nullable=False),
        sa.Column("baseline_cost", sa.Float(), nullable=True),
        sa.Column("savings", sa.Float(), nullable=False),
        sa.Column("savings_percent", sa.Float(), nullable=False),
        sa.Column("solver_method", sa.String(length=50), nullable=False),
        sa.Column("allocations", json_type, nullable=False),
        sa.Column("unfulfilled", json_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["procurement_sessions.id"],
            name=op.f("fk_optimization_results_session_id_procurement_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_optimization_results")),
    )
    op.create_index(
        op.f("ix_optimization_results_session_id"),
        "optimization_results",
        ["session_id"],
        unique=True,
    )


def downgrade() -> None:
    """Откат таблиц оптимизации, потребности и поставщиков."""
    op.drop_index(op.f("ix_optimization_results_session_id"), table_name="optimization_results")
    op.drop_table("optimization_results")

    op.drop_index(op.f("ix_demand_items_tire_id"), table_name="demand_items")
    op.drop_index(op.f("ix_demand_items_session_id"), table_name="demand_items")
    op.drop_table("demand_items")

    with op.batch_alter_table("raw_offer_items", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_raw_offer_items_supplier_id_suppliers"),
            type_="foreignkey",
        )
        batch_op.drop_index(batch_op.f("ix_raw_offer_items_supplier_id"))
        batch_op.drop_column("min_order_qty")
        batch_op.drop_column("lot_size")
        batch_op.drop_column("supplier_id")

    op.drop_table("suppliers")
