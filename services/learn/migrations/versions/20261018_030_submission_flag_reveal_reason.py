"""제출 열람 감사에 사유를 추가하고 이전 감사 행의 NULL을 보존한다."""

from alembic import op
import sqlalchemy as sa


revision = "20261018_030"
down_revision = "20261013_029"
branch_labels = None
depends_on = None

_TABLE = "submission_flag_reveal_audits"


def upgrade():
    op.add_column(_TABLE, sa.Column("reason", sa.Text(), nullable=True))


def downgrade():
    bind = op.get_bind()
    # 같은 transaction에서 확인과 DROP 사이의 새 사유 기록을 막는다.
    bind.execute(sa.text("LOCK TABLE submission_flag_reveal_audits IN ACCESS EXCLUSIVE MODE"))
    reason_count = bind.execute(
        sa.text("SELECT count(*) FROM submission_flag_reveal_audits WHERE reason IS NOT NULL")
    ).scalar_one()
    if reason_count:
        raise RuntimeError("제출 원문 열람 사유가 있어 destructive downgrade를 중단합니다.")
    op.drop_column(_TABLE, "reason")
