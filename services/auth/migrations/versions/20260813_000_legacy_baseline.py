"""Auth legacy-core baseline marker.

Revision ``000`` does not recreate historical Auth core tables.  The supported
``app.scripts.auth_migrate`` wrapper validates the revision-000 core contract
and then stamps this marker before it applies the forward migration chain.
Direct ``alembic upgrade`` is not a replacement for that wrapper.

Revision ID: 20260813_000
Revises:
Create Date: 2026-08-13
"""

revision = "20260813_000"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical core ownership remains outside Alembic.  The wrapper performs
    # the pre-stamp contract validation because `stamp` intentionally skips
    # Alembic revision functions.
    pass


def downgrade() -> None:
    pass
