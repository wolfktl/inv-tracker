"""suppliers directory + equipment inner_name and supplier_id

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

STR = lambda n: sa.String(length=n)
DEF = {"server_default": "", "nullable": False}


def upgrade():
    op.create_table(
        "suppliers",
        sa.Column("id", STR(64), primary_key=True),
        sa.Column("name", STR(200), **DEF),
        sa.Column("contact", STR(200), **DEF),
        sa.Column("phone", STR(64), **DEF),
        sa.Column("email", STR(128), **DEF),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
    )
    op.add_column("equipment", sa.Column("inner_name", STR(300), server_default="", nullable=False))
    op.add_column("equipment", sa.Column("supplier_id", STR(64), nullable=True))


def downgrade():
    op.drop_column("equipment", "supplier_id")
    op.drop_column("equipment", "inner_name")
    op.drop_table("suppliers")
