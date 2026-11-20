"""split employee full_name into last/first/middle parts

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

STR = lambda n: sa.String(length=n)
DEF = {"server_default": "", "nullable": False}


def upgrade():
    op.add_column("employees", sa.Column("last_name", STR(80), **DEF))
    op.add_column("employees", sa.Column("first_name", STR(80), **DEF))
    op.add_column("employees", sa.Column("middle_name", STR(80), **DEF))

    # Разбираем существующие ФИО на части по пробелам.
    emps = sa.table(
        "employees",
        sa.column("id", sa.String),
        sa.column("full_name", sa.String),
        sa.column("last_name", sa.String),
        sa.column("first_name", sa.String),
        sa.column("middle_name", sa.String),
    )
    conn = op.get_bind()
    for row in conn.execute(sa.select(emps.c.id, emps.c.full_name)):
        parts = (row.full_name or "").split()
        last = parts[0] if parts else ""
        first = parts[1] if len(parts) > 1 else ""
        middle = " ".join(parts[2:]) if len(parts) > 2 else ""
        if last or first or middle:
            conn.execute(
                emps.update()
                .where(emps.c.id == row.id)
                .values(last_name=last, first_name=first, middle_name=middle)
            )


def downgrade():
    op.drop_column("employees", "middle_name")
    op.drop_column("employees", "first_name")
    op.drop_column("employees", "last_name")
