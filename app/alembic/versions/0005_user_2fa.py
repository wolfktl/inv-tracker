"""user 2FA (TOTP) fields

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

JSON_T = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.add_column("users", sa.Column("totp_secret", sa.String(length=64), server_default="", nullable=False))
    op.add_column("users", sa.Column("totp_enabled", sa.Integer(), server_default="0", nullable=False))
    op.add_column("users", sa.Column("recovery_codes", JSON_T, nullable=True))


def downgrade():
    op.drop_column("users", "recovery_codes")
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret")
