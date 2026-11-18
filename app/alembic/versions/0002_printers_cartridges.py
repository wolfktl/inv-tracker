"""printers and cartridges

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

STR = lambda n: sa.String(length=n)
DEF = {"server_default": "", "nullable": False}


def upgrade():
    op.create_table(
        "printers",
        sa.Column("id", STR(64), primary_key=True),
        sa.Column("name", STR(200), **DEF),
        sa.Column("model", STR(200), **DEF),
        sa.Column("location", STR(128), **DEF),
        sa.Column("inv_no", STR(64), **DEF),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
    )
    op.create_table(
        "cartridges",
        sa.Column("id", STR(64), primary_key=True),
        sa.Column("name", STR(200), **DEF),
        sa.Column("printer_id", STR(64), sa.ForeignKey("printers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("stock", sa.Integer(), server_default="0", nullable=False),
        sa.Column("min_stock", sa.Integer(), server_default="0", nullable=False),
        sa.Column("alerted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
    )


def downgrade():
    op.drop_table("cartridges")
    op.drop_table("printers")
