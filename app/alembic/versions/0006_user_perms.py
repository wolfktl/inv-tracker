"""user section permissions

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

JSON_T = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.add_column("users", sa.Column("perms", JSON_T, nullable=True))


def downgrade():
    op.drop_column("users", "perms")
