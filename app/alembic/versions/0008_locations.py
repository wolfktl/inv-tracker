"""locations directory + tsd.location

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

STR = lambda n: sa.String(length=n)
DEF = {"server_default": "", "nullable": False}


def upgrade():
    op.create_table(
        "locations",
        sa.Column("id", STR(64), primary_key=True),
        sa.Column("name", STR(200), **DEF),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
    )
    op.add_column("tsd", sa.Column("location", STR(128), server_default="", nullable=False))


def downgrade():
    op.drop_column("tsd", "location")
    op.drop_table("locations")
