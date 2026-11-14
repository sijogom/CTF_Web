"""Inventory, apply, or roll back Auth canonical HTML conversion."""

from app.config import settings
from app.database import engine
from app.models.competition import Competition
from app.models.notification import Notification
from app.models.team import Team
from app.models.user import User
from app.utils.rich_content_migration import RichContentFieldSpec, run_cli


SPECS = (
    RichContentFieldSpec(
        User.__table__,
        "bio",
        field_name="자기소개",
        allow_empty=True,
        max_visible_chars=500,
        nullable=True,
        guard_columns=("role", "is_active", "created_at", "updated_at"),
    ),
    RichContentFieldSpec(
        Team.__table__,
        "description",
        field_name="팀 소개",
        allow_empty=True,
        nullable=True,
        guard_columns=("captain_id", "created_at"),
    ),
    RichContentFieldSpec(
        Notification.__table__,
        "content",
        field_name="알림 내용",
        allow_empty=True,
        max_visible_chars=5000,
        guard_columns=("type", "created_by", "created_at"),
    ),
    RichContentFieldSpec(
        Competition.__table__,
        "description",
        field_name="대회 설명",
        allow_empty=True,
        nullable=True,
        guard_columns=("created_by", "is_published", "is_deleted", "created_at"),
    ),
)


if __name__ == "__main__":
    run_cli(
        service="auth",
        environment=settings.environment,
        engine=engine,
        specs=SPECS,
    )
