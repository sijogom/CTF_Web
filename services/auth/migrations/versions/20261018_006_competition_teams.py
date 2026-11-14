"""고정 명단 팀전의 별도 원장을 추가하고 기존 NULL 계약은 보존한다."""

import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg


revision = "20261018_006"
down_revision = "20261013_005"
branch_labels = None
depends_on = None

_TABLES = (
    "competition_team_registrations", "competition_team_members",
    "competition_team_assignments", "competition_team_solves",
)
_LEGACY_TABLES = ("competition_participants", "competition_assignments")
_GUARD = "guard_fixed_roster_legacy_write"


def _target_schema():
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    if not isinstance(schema, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema) is None:
        raise RuntimeError("팀전 마이그레이션의 대상 schema를 확인할 수 없습니다.")
    return bind, schema, bind.dialect.identifier_preparer.quote_schema(schema)


def _registration_fk(schema):
    return sa.ForeignKeyConstraint(
        ["competition_id", "team_id"],
        [f"{schema}.competition_team_registrations.competition_id",
         f"{schema}.competition_team_registrations.team_id"],
        ondelete="CASCADE",
    )


def upgrade():
    bind, schema, quoted = _target_schema()
    inspector = sa.inspect(bind)
    existing = next((column for column in inspector.get_columns("competitions", schema=schema)
                     if column["name"] == "team_contract"), None)
    if existing is None:
        op.add_column("competitions", sa.Column("team_contract", sa.String(30), nullable=True), schema=schema)
    else:
        if (not isinstance(existing["type"], sa.String) or existing["type"].length != 30
                or not existing["nullable"] or existing.get("default") is not None):
            raise RuntimeError("기존 team_contract의 타입·NULL·기본값 계약이 다릅니다.")
        # 빈 DB bootstrap의 현재 metadata가 이미 만든 CHECK도 같은 DDL TX에서 재검증한다.
        if any(check["name"] == "ck_competitions_team_contract"
               for check in inspector.get_check_constraints("competitions", schema=schema)):
            op.drop_constraint("ck_competitions_team_contract", "competitions", type_="check", schema=schema)
    op.create_check_constraint(
        "ck_competitions_team_contract", "competitions",
        "team_contract IS NULL OR (team_contract = 'fixed_roster_v1' AND mode = 'team')",
        schema=schema,
    )
    op.create_table(
        "competition_team_registrations",
        sa.Column("competition_id", pg.UUID(as_uuid=True), sa.ForeignKey(f"{schema}.competitions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("team_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("team_name_snapshot", sa.String(100), nullable=False),
        sa.Column("registered_by", pg.UUID(as_uuid=True), sa.ForeignKey(f"{schema}.users.id"), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_table(
        "competition_team_members",
        sa.Column("competition_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("team_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey(f"{schema}.users.id"), primary_key=True),
        sa.Column("username_snapshot", sa.String(50), nullable=False),
        _registration_fk(schema),
        sa.UniqueConstraint("competition_id", "user_id", name="uq_competition_team_members_competition_user"),
        schema=schema,
    )
    op.create_table(
        "competition_team_assignments",
        sa.Column("competition_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("team_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("category", sa.String(50), primary_key=True),
        sa.Column("challenge_ids", pg.ARRAY(sa.Integer), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        _registration_fk(schema),
        schema=schema,
    )
    op.create_table(
        "competition_team_solves",
        sa.Column("competition_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("team_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("challenge_id", sa.Integer, primary_key=True),
        sa.Column("solved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_solver_id", pg.UUID(as_uuid=True), sa.ForeignKey(f"{schema}.users.id"), nullable=False),
        _registration_fk(schema),
        schema=schema,
    )
    op.create_index("ix_competition_team_solves_competition_time", "competition_team_solves",
                    ["competition_id", "solved_at"], schema=schema)
    # Invoker 권한으로 같은 schema의 부모 행을 잠가 marker 변경과도 직렬화한다.
    bind.execute(sa.text(f"""
        CREATE FUNCTION {quoted}.{_GUARD}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog AS $$
        DECLARE old_competition uuid; contract text;
        BEGIN
            IF TG_OP = 'UPDATE' THEN old_competition := OLD.competition_id; END IF;
            FOR contract IN
                SELECT team_contract FROM {quoted}.competitions
                WHERE id IN (NEW.competition_id, old_competition)
                ORDER BY id FOR SHARE
            LOOP
                IF contract = 'fixed_roster_v1' THEN
                    RAISE EXCEPTION '고정 명단 팀전은 개인 참가·배정 원장에 쓸 수 없습니다.'
                        USING ERRCODE = '23514';
                END IF;
            END LOOP;
            RETURN NEW;
        END;
        $$
    """))
    for table in _LEGACY_TABLES:
        bind.execute(sa.text(f"""
            CREATE TRIGGER {table}_fixed_roster_guard
            BEFORE INSERT OR UPDATE ON {quoted}.{table}
            FOR EACH ROW EXECUTE FUNCTION {quoted}.{_GUARD}()
        """))


def downgrade():
    bind, schema, quoted = _target_schema()
    tables = ("competitions", *_TABLES, *_LEGACY_TABLES)
    bind.execute(sa.text("LOCK TABLE " + ", ".join(f"{quoted}.{table}" for table in tables)
                         + " IN ACCESS EXCLUSIVE MODE"))
    checks = [f"EXISTS (SELECT 1 FROM {quoted}.competitions WHERE team_contract IS NOT NULL)"]
    checks.extend(f"EXISTS (SELECT 1 FROM {quoted}.{table})" for table in _TABLES)
    if bind.execute(sa.text("SELECT " + " OR ".join(checks))).scalar_one():
        raise RuntimeError("고정 팀전 계약 또는 원장이 있어 downgrade로 제거할 수 없습니다.")
    for table in _LEGACY_TABLES:
        bind.execute(sa.text(f"DROP TRIGGER {table}_fixed_roster_guard ON {quoted}.{table}"))
    bind.execute(sa.text(f"DROP FUNCTION {quoted}.{_GUARD}()"))
    op.drop_index("ix_competition_team_solves_competition_time", table_name="competition_team_solves", schema=schema)
    for table in reversed(_TABLES):
        op.drop_table(table, schema=schema)
    op.drop_constraint("ck_competitions_team_contract", "competitions", type_="check", schema=schema)
    op.drop_column("competitions", "team_contract", schema=schema)
