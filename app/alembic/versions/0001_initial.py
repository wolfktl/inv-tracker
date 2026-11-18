"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# JSONB на PostgreSQL, обычный JSON на остальных СУБД.
JSON_T = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
STR = lambda n: sa.String(length=n)
DEF = {"server_default": "", "nullable": False}


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", STR(64), nullable=False),
        sa.Column("password_hash", STR(255), nullable=False),
        sa.Column("full_name", STR(128), server_default="", nullable=False),
        sa.Column("role", STR(32), server_default="admin", nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "employees",
        sa.Column("id", STR(64), primary_key=True),
        sa.Column("full_name", STR(200), **DEF),
        sa.Column("login", STR(64), **DEF),
        sa.Column("email", STR(128), **DEF),
        sa.Column("dept", STR(128), **DEF),
        sa.Column("position", STR(128), **DEF),
        sa.Column("location", STR(128), **DEF),
        sa.Column("passport", STR(64), **DEF),
        sa.Column("passport_issued", STR(255), **DEF),
        sa.Column("passport_code", STR(32), **DEF),
        sa.Column("contract_no", STR(64), **DEF),
        sa.Column("contract_date", STR(32), **DEF),
    )

    op.create_table(
        "equipment",
        sa.Column("id", STR(64), primary_key=True),
        sa.Column("type", STR(32), server_default="Прочее", nullable=False),
        sa.Column("name", STR(300), **DEF),
        sa.Column("invno", STR(64), **DEF),
        sa.Column("serial", STR(128), **DEF),
        sa.Column("purchase", STR(200), **DEF),
        sa.Column("status", STR(16), server_default="stock", nullable=False),
        sa.Column("emp_id", STR(64), sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True),
        sa.Column("holder_text", STR(200), **DEF),
        sa.Column("workplace", STR(200), **DEF),
        sa.Column("assign_type", STR(16), **DEF),
        sa.Column("cost", STR(32), **DEF),
        sa.Column("location", STR(128), **DEF),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "acts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("no", sa.Integer(), nullable=False),
        sa.Column("kind", STR(16), nullable=False),
        sa.Column("date", STR(16), nullable=False),
        sa.Column("emp_id", STR(64), nullable=True),
        sa.Column("emp_name", STR(200), server_default="", nullable=False),
        sa.Column("items", JSON_T, nullable=True),
        sa.Column("created_by", STR(64), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "settings",
        sa.Column("key", STR(64), primary_key=True),
        sa.Column("value", JSON_T, nullable=True),
    )


def downgrade():
    op.drop_table("settings")
    op.drop_table("acts")
    op.drop_table("equipment")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("employees")
    op.drop_table("users")
