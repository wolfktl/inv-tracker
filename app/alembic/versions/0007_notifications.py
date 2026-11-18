"""notifications directory + printer.notify_id

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

STR = lambda n: sa.String(length=n)
DEF = {"server_default": "", "nullable": False}


def upgrade():
    op.create_table(
        "notifications",
        sa.Column("id", STR(64), primary_key=True),
        sa.Column("name", STR(200), **DEF),
        sa.Column("telegram_chat_id", STR(64), **DEF),
        sa.Column("email", STR(128), **DEF),
        sa.Column("enabled", sa.Integer(), server_default="1", nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
    )
    op.add_column("printers", sa.Column("notify_id", STR(64), nullable=True))


def downgrade():
    op.drop_column("printers", "notify_id")
    op.drop_table("notifications")
