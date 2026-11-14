"""Restore model-declared indexes omitted by earlier legacy migrations."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260814_023"
down_revision = "20260814_022"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_challenge_environment_verifications_challenge_id", "challenge_environment_verifications", ("challenge_id",)),
    ("ix_courses_deleted_by", "courses", ("deleted_by",)),
    ("ix_education_asset_cleanup_jobs_asset_id", "education_asset_cleanup_jobs", ("asset_id",)),
    ("ix_education_asset_cleanup_jobs_available_at", "education_asset_cleanup_jobs", ("available_at",)),
    ("ix_education_asset_cleanup_jobs_lease_until", "education_asset_cleanup_jobs", ("lease_until",)),
    ("ix_education_asset_cleanup_jobs_status", "education_asset_cleanup_jobs", ("status",)),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for name, table, columns in _INDEXES:
        existing = {item.get("name") for item in inspector.get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, list(columns))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for name, table, _columns in reversed(_INDEXES):
        existing = {item.get("name") for item in inspector.get_indexes(table)}
        if name in existing:
            op.drop_index(name, table_name=table)
