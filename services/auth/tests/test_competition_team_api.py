"""고정 명단 팀전의 추가 응답과 기존 개인전 보존 경계."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.api import competition, teams
from app.models.competition import Competition


NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
COMP_ID = UUID("00000000-0000-4000-8000-000000000001")
TEAM_ID = UUID("00000000-0000-4000-8000-000000000002")
USER_ID = UUID("00000000-0000-4000-8000-000000000003")


class _Count:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        if self.value is None:
            raise AssertionError("EXPECTED_ONE_ROW")
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value


class _Session:
    def __init__(self, *, counts=()):
        self.added = []
        self.counts = list(counts)
        self.commits = 0
        self.statements = []
        self.deleted = []

    def add(self, value):
        self.added.append(value)

    async def execute(self, statement):
        statement.compile(dialect=postgresql.dialect())
        self.statements.append(statement)
        if not self.counts:
            raise AssertionError("UNEXPECTED_DATABASE_CALL")
        return _Count(self.counts.pop(0))

    async def flush(self):
        for value in self.added:
            if isinstance(value, Competition):
                value.id = COMP_ID
                value.is_published = False
                value.is_deleted = False
                value.deleted_at = None

    async def refresh(self, _value):
        return None

    async def commit(self):
        self.commits += 1

    async def delete(self, value):
        self.deleted.append(value)


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


class CompetitionTeamApiTests(unittest.IsolatedAsyncioTestCase):
    def comp(self, **changes):
        values = dict(id=COMP_ID, title="대회", description=None, mode="team", team_contract="fixed_roster_v1",
                      start_at=NOW - timedelta(hours=1), end_at=NOW + timedelta(hours=1), freeze_minutes=30,
                      is_published=True, is_deleted=False, deleted_at=None, banner_url=None)
        values.update(changes)
        return SimpleNamespace(**values)

    def team_context(self):
        registration = SimpleNamespace(competition_id=COMP_ID, team_id=TEAM_ID, team_name_snapshot="등록 팀",
                                       registered_at=NOW - timedelta(hours=2), registered_by=USER_ID)
        member = SimpleNamespace(competition_id=COMP_ID, team_id=TEAM_ID, user_id=USER_ID, username_snapshot="등록 이름")
        return SimpleNamespace(registration=registration, members=(member,), member=member, team_id=TEAM_ID)

    def assignment(self):
        return SimpleNamespace(category="web", challenge_ids=[7], assigned_at=NOW - timedelta(minutes=1))

    def challenge(self, *, kind="static"):
        return SimpleNamespace(challenge_id=7, category="web", difficulty="easy", scoring_type=kind,
                               initial_value=500, decay=10, minimum_value=50)

    def body(self, mode):
        return competition.CompetitionCreate(
            title="소유 테스트 대회", mode=mode,
            start_at=NOW + timedelta(hours=1), end_at=NOW + timedelta(hours=2),
        )

    async def test_only_new_team_create_gets_server_owned_marker(self):
        body = self.body("team")
        self.assertNotIn("team_contract", competition.CompetitionCreate.model_fields)
        session = _Session()
        result = await competition.create_competition(body, db=session, user=SimpleNamespace(id=USER_ID))
        created = next(value for value in session.added if isinstance(value, Competition))
        self.assertEqual(created.team_contract, "fixed_roster_v1")
        self.assertEqual(result.team_contract, "fixed_roster_v1")
        self.assertEqual(session.commits, 1)

    async def test_individual_create_keeps_nullable_contract(self):
        session = _Session()
        result = await competition.create_competition(self.body("individual"), db=session, user=SimpleNamespace(id=USER_ID))
        created = next(value for value in session.added if isinstance(value, Competition))
        self.assertIsNone(created.team_contract)
        self.assertIsNone(result.team_contract)
        self.assertEqual(session.commits, 1)

    def test_join_body_accepts_only_a_valid_team_uuid(self):
        body = competition.CompetitionJoinRequest(team_id=str(TEAM_ID))
        self.assertEqual(body.team_id, TEAM_ID)
        with self.assertRaises(ValidationError):
            competition.CompetitionJoinRequest(team_id="not-a-team-uuid")

    def test_registration_payload_uses_fixed_names_and_no_sensitive_fields(self):
        registration = SimpleNamespace(
            competition_id=COMP_ID, team_id=TEAM_ID, team_name_snapshot="등록 시 팀 이름",
            registered_at=NOW, registered_by=USER_ID,
        )
        member = SimpleNamespace(
            user_id=USER_ID, username_snapshot="등록 시 이름", email="must-not-be-returned@example.invalid",
        )
        self.assertEqual(competition._team_registration_payload(registration, [member]), {
            "team_id": str(TEAM_ID), "team_name": "등록 시 팀 이름", "registered_at": NOW.isoformat(),
            "members": [{"user_id": str(USER_ID), "username": "등록 시 이름"}],
        })

    async def test_same_mode_edit_does_not_reinterpret_legacy_or_add_queries(self):
        for mode, marker in (("individual", None), ("team", None), ("team", "fixed_roster_v1")):
            comp = SimpleNamespace(mode=mode, team_contract=marker, start_at=NOW - timedelta(hours=1), end_at=NOW + timedelta(hours=1))
            session = _Session()
            await competition._require_mode_change_allowed(session, comp, mode, NOW)
            self.assertEqual(comp.mode, mode)
            self.assertEqual(comp.team_contract, marker)
            self.assertEqual(session.commits, 0)

    async def test_started_or_ended_mode_change_is_rejected_without_mutation(self):
        for start_at, end_at in ((NOW, NOW + timedelta(hours=1)), (NOW - timedelta(hours=2), NOW)):
            comp = SimpleNamespace(mode="individual", team_contract=None, start_at=start_at, end_at=end_at)
            session = _Session()
            with self.assertRaises(HTTPException) as caught:
                await competition._require_mode_change_allowed(session, comp, "team", NOW)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(comp.mode, "individual")
            self.assertIsNone(comp.team_contract)
            self.assertEqual(session.commits, 0)

    async def test_fixed_marker_cannot_be_downgraded_by_an_upcoming_mode_patch(self):
        comp = SimpleNamespace(mode="team", team_contract="fixed_roster_v1", start_at=NOW + timedelta(hours=1), end_at=NOW + timedelta(hours=2))
        session = _Session()
        with self.assertRaises(HTTPException) as caught:
            await competition._require_mode_change_allowed(session, comp, "individual", NOW)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(comp.team_contract, "fixed_roster_v1")
        self.assertEqual(comp.mode, "team")
        self.assertEqual(session.commits, 0)

    async def test_upcoming_legacy_rows_block_mode_change_without_marker_promotion(self):
        for counts in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
            comp = SimpleNamespace(id=COMP_ID, mode="individual", team_contract=None, start_at=NOW + timedelta(hours=1), end_at=NOW + timedelta(hours=2))
            session = _Session(counts=counts)
            with self.subTest(counts=counts), self.assertRaises(HTTPException) as caught:
                await competition._require_mode_change_allowed(session, comp, "team", NOW)
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(comp.mode, "individual")
            self.assertIsNone(comp.team_contract)
            self.assertEqual(session.commits, 0)

    async def test_empty_upcoming_legacy_mode_check_does_not_promote_marker_or_commit(self):
        comp = SimpleNamespace(id=COMP_ID, mode="individual", team_contract=None, start_at=NOW + timedelta(hours=1), end_at=NOW + timedelta(hours=2))
        session = _Session(counts=(0, 0, 0))
        await competition._require_mode_change_allowed(session, comp, "team", NOW)
        self.assertIsNone(comp.team_contract)
        self.assertEqual(comp.mode, "individual")
        self.assertEqual(session.commits, 0)

    async def test_team_join_body_is_required_before_any_registration_write(self):
        session = _Session(counts=[self.comp()])
        with patch.object(competition, "datetime", _Clock), patch.object(competition.team_service, "register_team", new=AsyncMock()) as register:
            with self.assertRaises(HTTPException) as caught:
                await competition.join_competition(COMP_ID, db=session, user=SimpleNamespace(id=USER_ID))
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(session.commits, 0)
        register.assert_not_awaited()

    async def test_team_join_returns_fixed_snapshot_and_never_creates_personal_participant(self):
        context = self.team_context()
        session = _Session(counts=[self.comp()])
        with patch.object(competition, "datetime", _Clock), \
             patch.object(competition.team_service, "register_team", new=AsyncMock(return_value=context.registration)) as register, \
             patch.object(competition.team_service, "resolve_team_context", new=AsyncMock(return_value=context)):
            result = await competition.join_competition(COMP_ID, competition.CompetitionJoinRequest(team_id=TEAM_ID), db=session, user=SimpleNamespace(id=USER_ID))
        register.assert_awaited_once_with(session, COMP_ID, TEAM_ID, USER_ID)
        self.assertEqual(result["team_registration"], competition._team_registration_payload(context.registration, context.members))
        self.assertEqual(session.added, [])
        self.assertEqual(session.commits, 1)
        self.assertIsNotNone(session.statements[0]._for_update_arg)
        self.assertTrue(session.statements[0].get_execution_options()["populate_existing"])

    async def test_registration_domain_denial_is_preserved_and_not_committed(self):
        error = competition.team_service.CompetitionTeamError(403, "captain_required", "팀장만 등록할 수 있습니다.")
        session = _Session(counts=[self.comp()])
        with patch.object(competition, "datetime", _Clock), patch.object(competition.team_service, "register_team", new=AsyncMock(side_effect=error)):
            with self.assertRaises(HTTPException) as caught:
                await competition.join_competition(COMP_ID, competition.CompetitionJoinRequest(team_id=TEAM_ID), db=session, user=SimpleNamespace(id=USER_ID))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(session.commits, 0)
        self.assertEqual(session.added, [])

    async def test_nullable_legacy_team_join_keeps_bodyless_personal_contract(self):
        session = _Session(counts=[self.comp(team_contract=None), None])
        with patch.object(competition, "datetime", _Clock), patch.object(competition.team_service, "register_team", new=AsyncMock()) as register:
            result = await competition.join_competition(COMP_ID, db=session, user=SimpleNamespace(id=USER_ID))
        self.assertEqual(result, {"message": "대회에 참가했습니다."})
        self.assertEqual(len(session.added), 1)
        self.assertEqual(session.added[0].__tablename__, "competition_participants")
        self.assertEqual(session.commits, 1)
        register.assert_not_awaited()

    async def test_existing_common_assignment_returns_before_learn_pool_read(self):
        comp = self.comp()
        session = _Session(counts=[comp, comp, self.assignment()])
        with patch.object(competition, "datetime", _Clock), \
             patch.object(competition.team_service, "resolve_team_context", new=AsyncMock(return_value=self.team_context())), \
             patch.object(competition, "_fetch_challenge_info", new=AsyncMock()) as fetch:
            result = await competition.select_category(COMP_ID, category="web", db=session, user=SimpleNamespace(id=USER_ID))
        self.assertEqual(result, {"category": "web", "challenge_ids": [7], "already_assigned": True})
        fetch.assert_not_awaited()
        self.assertEqual(session.commits, 0)
        self.assertIsNotNone(session.statements[1]._for_update_arg)
        self.assertTrue(session.statements[1].get_execution_options()["populate_existing"])

    async def test_new_common_assignment_passes_only_eligible_ctf_ids_to_service(self):
        comp = self.comp()
        session = _Session(counts=[comp, comp, None, [self.challenge()]])
        info = {7: {"is_published": True, "challenge_type": "competition", "env_type": "none"}}
        assigned = SimpleNamespace(assignment=self.assignment(), already_assigned=False)
        with patch.object(competition, "datetime", _Clock), \
             patch.object(competition.team_service, "resolve_team_context", new=AsyncMock(return_value=self.team_context())), \
             patch.object(competition.team_service, "get_or_create_team_assignment", new=AsyncMock(return_value=assigned)) as assign, \
             patch.object(competition, "_fetch_challenge_info", new=AsyncMock(return_value=info)):
            result = await competition.select_category(COMP_ID, category="web", db=session, user=SimpleNamespace(id=USER_ID))
        assign.assert_awaited_once_with(session, COMP_ID, USER_ID, "web", [7])
        self.assertEqual(result, {"category": "web", "challenge_ids": [7], "already_assigned": False})
        self.assertEqual(session.commits, 1)

    async def test_detail_separates_team_completion_from_personal_solved(self):
        context = self.team_context()
        session = _Session(counts=[self.comp(), 1, [self.assignment()], [self.challenge()],
                                   [SimpleNamespace(challenge_id=7, cnt=1)], [],
                                   [SimpleNamespace(challenge_id=7, first_blood_name="등록 팀")], [7]])
        info = {7: {"is_published": True, "challenge_type": "competition", "env_type": "none", "name": "문제", "description": "본문"}}
        with patch.object(competition, "datetime", _Clock), \
             patch.object(competition.team_service, "resolve_team_context", new=AsyncMock(return_value=context)), \
             patch.object(competition, "_fetch_challenge_info", new=AsyncMock(return_value=info)):
            result = await competition.get_competition(COMP_ID, db=session, user=SimpleNamespace(id=USER_ID, role="user"))
        self.assertTrue(result["joined"])
        self.assertEqual(result["team_contract"], "fixed_roster_v1")
        self.assertEqual(result["team_registration"]["team_name"], "등록 팀")
        self.assertIs(result["challenges"][0]["solved"], False)
        self.assertIs(result["challenges"][0]["team_solved"], True)
        self.assertEqual(result["challenges"][0]["solves"], 1)
        self.assertEqual(session.commits, 0)

    async def test_team_scoreboard_uses_one_team_row_not_personal_solve_count(self):
        context = self.team_context()
        solve = SimpleNamespace(team_id=TEAM_ID, challenge_id=7, solved_at=NOW - timedelta(minutes=1))
        rows = SimpleNamespace(registrations=(context.registration,), members=context.members, solves=(solve,))
        session = _Session(counts=[self.comp(), [self.challenge(kind="dynamic")]])
        with patch.object(competition, "datetime", _Clock), patch.object(competition.team_service, "load_team_scoring_rows", new=AsyncMock(return_value=rows)):
            result = await competition.scoreboard(COMP_ID, db=session)
        self.assertEqual(result, {"team_contract": "fixed_roster_v1", "is_frozen": False, "entries": [
            {"rank": 1, "team_id": str(TEAM_ID), "team_name": "등록 팀", "score": 496, "solves": 1, "first_bloods": 1},
        ]})
        self.assertEqual(session.commits, 0)

    async def test_deleted_admin_detail_keeps_snapshot_preview_without_reopening_access(self):
        context = self.team_context()
        rows = SimpleNamespace(registrations=(context.registration,), members=context.members, solves=())
        session = _Session(counts=[self.comp(is_deleted=True), 1, [self.assignment()], [self.challenge()],
                                   [], [], [], []])
        info = {7: {"is_published": True, "challenge_type": "competition", "env_type": "none", "name": "문제"}}
        with patch.object(competition, "datetime", _Clock), \
             patch.object(competition.team_service, "load_team_scoring_rows", new=AsyncMock(return_value=rows)), \
             patch.object(competition.team_service, "resolve_team_context", new=AsyncMock()) as resolve, \
             patch.object(competition, "_fetch_challenge_info", new=AsyncMock(return_value=info)):
            result = await competition.get_competition(COMP_ID, db=session, user=SimpleNamespace(id=USER_ID, role="admin"))
        self.assertTrue(result["is_deleted"])
        self.assertTrue(result["joined"])
        self.assertEqual(result["team_registration"]["members"][0]["username"], "등록 이름")
        resolve.assert_not_awaited()
        self.assertEqual(session.commits, 0)

    async def test_registered_team_list_uses_snapshots_only_and_legacy_returns_empty(self):
        context = self.team_context()
        rows = SimpleNamespace(registrations=(context.registration,), members=context.members, solves=())
        with patch.object(competition.team_service, "load_team_scoring_rows", new=AsyncMock(return_value=rows)) as load:
            result = await competition.registered_competition_teams(COMP_ID, db=_Session(counts=[self.comp()]))
            self.assertEqual(result, [competition._team_registration_payload(context.registration, context.members)])
            load.reset_mock()
            self.assertEqual(await competition.registered_competition_teams(COMP_ID, db=_Session(counts=[self.comp(team_contract=None)])), [])
            load.assert_not_awaited()

    async def test_registered_teams_never_exposes_private_or_deleted_competition(self):
        for comp in (self.comp(is_published=False), self.comp(is_deleted=True)):
            with self.subTest(comp=comp), patch.object(competition.team_service, "load_team_scoring_rows", new=AsyncMock()) as load:
                with self.assertRaises(HTTPException) as caught:
                    await competition.registered_competition_teams(COMP_ID, db=_Session(counts=[comp]))
                self.assertEqual(caught.exception.status_code, 404)
                load.assert_not_awaited()

    async def test_team_graph_uses_same_strict_freeze_cutoff_and_snapshot_name(self):
        comp = self.comp(end_at=NOW + timedelta(minutes=15))
        cutoff = comp.end_at - timedelta(minutes=30)
        context = self.team_context()
        solve = SimpleNamespace(team_id=TEAM_ID, challenge_id=7, solved_at=cutoff - timedelta(microseconds=1))
        rows = SimpleNamespace(registrations=(context.registration,), members=context.members, solves=(solve,))
        session = _Session(counts=[comp, [self.challenge(kind="dynamic")]])
        with patch.object(competition, "datetime", _Clock), patch.object(competition.team_service, "load_team_scoring_rows", new=AsyncMock(return_value=rows)) as load:
            result = await competition.scoreboard_graph(COMP_ID, db=session)
        load.assert_awaited_once_with(session, COMP_ID, cutoff=cutoff)
        self.assertEqual(result["team_contract"], "fixed_roster_v1")
        self.assertEqual(result["series"], [{"team_id": str(TEAM_ID), "team_name": "등록 팀", "data": [
            {"t": comp.start_at.isoformat(), "score": 0}, {"t": solve.solved_at.isoformat(), "score": 496},
        ]}])

    async def test_global_team_join_uses_same_team_lock_before_membership_write(self):
        team = SimpleNamespace(id=TEAM_ID, invite_code="synthetic-invite", captain_id=USER_ID)
        session = _Session(counts=[None])
        with patch.object(teams.team_service, "lock_team_for_membership", new=AsyncMock(return_value=team)) as lock:
            result = await teams.join_team(TEAM_ID, body=teams.TeamJoinRequest(invite_code="synthetic-invite"), db=session, user=SimpleNamespace(id=USER_ID))
        lock.assert_awaited_once_with(session, TEAM_ID)
        self.assertEqual(result, {"message": "팀에 가입했습니다."})
        self.assertEqual(session.added[0].team_id, TEAM_ID)
        self.assertEqual(session.commits, 1)

    async def test_global_team_removal_uses_same_team_lock_and_preserves_captain_rule(self):
        target_id = UUID("00000000-0000-4000-8000-000000000004")
        team = SimpleNamespace(id=TEAM_ID, captain_id=USER_ID)
        member = SimpleNamespace(team_id=TEAM_ID, user_id=target_id)
        session = _Session(counts=[member])
        with patch.object(teams.team_service, "lock_team_for_membership", new=AsyncMock(return_value=team)) as lock:
            result = await teams.remove_member(TEAM_ID, target_id, db=session, user=SimpleNamespace(id=USER_ID))
        lock.assert_awaited_once_with(session, TEAM_ID)
        self.assertEqual(result, {"message": "멤버가 제거되었습니다."})
        self.assertEqual(session.deleted, [member])
        self.assertEqual(session.commits, 1)
        session = _Session()
        with patch.object(teams.team_service, "lock_team_for_membership", new=AsyncMock(return_value=team)):
            with self.assertRaises(HTTPException) as caught:
                await teams.remove_member(TEAM_ID, USER_ID, db=session, user=SimpleNamespace(id=USER_ID))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(session.deleted, [])
        self.assertEqual(session.commits, 0)


if __name__ == "__main__":
    unittest.main()
