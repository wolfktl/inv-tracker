"""ТСД directory (terminals)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

STR = lambda n: sa.String(length=n)
DEF = {"server_default": "", "nullable": False}


def upgrade():
    op.create_table(
        "tsd",
        sa.Column("id", STR(64), primary_key=True),
        sa.Column("inv_no", STR(64), **DEF),      # инвентарный номер
        sa.Column("name", STR(200), **DEF),       # наименование
        sa.Column("inner_no", STR(64), **DEF),    # внутренний номер
        sa.Column("note", sa.Text(), server_default="", nullable=False),
    )


def downgrade():
    op.drop_table("tsd")
