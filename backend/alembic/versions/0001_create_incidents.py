"""Crée la table incidents.

Revision ID: 0001
Revises:
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    # VARCHAR + contrainte CHECK (pas de type ENUM PostgreSQL), comme dans app/db/models.py.
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=20)


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("source", _enum("incidentsource", "dataset", "live"), nullable=False),
        sa.Column("dataset_name", sa.String(length=100), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            _enum("incidentstatus", "pending", "running", "completed", "failed"),
            nullable=False,
        ),
        sa.Column("agent_statuses", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("window_end > window_start", name="ck_incidents_window_order"),
        sa.CheckConstraint(
            "source <> 'dataset' OR dataset_name IS NOT NULL",
            name="ck_incidents_dataset_name_required",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_incidents_created_at", "incidents", ["created_at"])
    op.create_index("ix_incidents_status", "incidents", ["status"])


def downgrade() -> None:
    op.drop_index("ix_incidents_status", table_name="incidents")
    op.drop_index("ix_incidents_created_at", table_name="incidents")
    op.drop_table("incidents")
