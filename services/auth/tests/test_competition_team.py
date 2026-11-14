"""Fixed-roster service contracts; no database, HTTP, or session factory."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.exc import IntegrityError

from app.models import competition as models


SERVICE = Path(__file__).parents[1] / "app/services/competition_team.py"
NOW = datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc)


class Result:
    def __init__(self, value=None, *, rowcount=0):
        self.value, self.rowcount = value, rowcount

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value

    def scalar(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def one_or_none(self):
        return self.value


class Session:
    def __init__(self, *results, flush_error=None):
        self.results = list(results)
        self.statements, self.added = [], []
        self.flush_error, self.flushes = flush_error, 0
        self.commits = self.rollbacks = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.results:
            raise AssertionError("unexpected database query")
        result = self.results.pop(0)
        return result if isinstance(result, Result) else Result(result)

    def add(self, row):
        self.added.append(row)

    def add_all(self, rows):
        self.added.extend(rows)

    async def flush(self):
        self.flushes += 1
        if self.flush_error:
            raise self.flush_error

    def in_transaction(self):
        return True

    async def commit(self):
        self.commits += 1
        raise AssertionError("the caller owns commit")

    async def rollback(self):
        self.rollbacks += 1
        raise AssertionError("the caller owns rollback")


def model(name, **values):
    cls = getattr(models, name, None)
    if cls is None:
        raise AssertionError(f"missing fixed-roster model: {name}")
    return cls(**values)


def sql(statement):
    return " ".join(str(statement.compile(dialect=dialect())).split())


class CompetitionTeamServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = importlib.import_module("app.services.competition_team")
        self.comp_id, self.team_id, self.captain, self.member = [uuid4() for _ in range(4)]
        self.comp = models.Competition(id=self.comp_id, title="owned competition", mode="team",
            team_contract="fixed_roster_v1", start_at=NOW - timedelta(hours=1),
            end_at=NOW + timedelta(hours=1), created_by=self.captain,
            is_deleted=False, is_published=True)
        self.registration = model("CompetitionTeamRegistration", competition_id=self.comp_id,
            team_id=self.team_id, registered_by=self.captain, registered_at=NOW,
            team_name_snapshot="original team")
        self.roster = [model("CompetitionTeamMember", competition_id=self.comp_id,
            team_id=self.team_id, user_id=user_id, username_snapshot=name)
            for user_id, name in ((self.captain, "captain at registration"), (self.member, "member at registration"))]
        self.context = self.service.TeamContext(self.registration, self.roster[1], tuple(self.roster))

    def assert_caller_transaction(self, db):
        self.assertEqual((db.commits, db.rollbacks), (0, 0))

    def test_marker_does_not_promote_legacy_team_or_individual(self):
        for mode, marker, expected in (("team", "fixed_roster_v1", True), ("team", None, False),
                ("individual", None, False), ("individual", "fixed_roster_v1", False), ("team", "other", False)):
            with self.subTest(mode=mode, marker=marker):
                self.assertIs(self.service.is_fixed_roster_team(SimpleNamespace(mode=mode, team_contract=marker)), expected)

    async def test_membership_mutation_locks_live_team_without_committing(self):
        team = SimpleNamespace(id=self.team_id, captain_id=self.captain, name="live")
        db = Session(team)
        self.assertIs(await self.service.lock_team_for_membership(db, self.team_id), team)
        self.assertIn("FROM teams", sql(db.statements[0]))
        self.assertIn("FOR UPDATE", sql(db.statements[0]))
        self.assertTrue(db.statements[0].get_execution_options()["populate_existing"])
        self.assert_caller_transaction(db)

    async def test_missing_live_team_is_404(self):
        db = Session(None)
        with self.assertRaises(self.service.CompetitionTeamError) as caught:
            await self.service.lock_team_for_membership(db, uuid4())
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(db.added, [])

    async def test_legacy_team_cannot_enter_fixed_roster_service(self):
        self.comp.team_contract = None
        db = Session(self.comp)
        with self.assertRaises(self.service.CompetitionTeamError) as caught:
            await self.service.register_team(db, self.comp_id, self.team_id, self.captain)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(db.added, [])

    async def test_soft_deleted_competition_is_not_resolved(self):
        self.comp.is_deleted = True
        db = Session(self.comp)
        with self.assertRaises(self.service.CompetitionTeamError) as caught:
            await self.service.resolve_team_context(db, self.comp_id, self.member)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(len(db.statements), 1)

    async def test_registration_snapshots_all_members_and_name_competition_first(self):
        team = SimpleNamespace(id=self.team_id, captain_id=self.captain, name="original team")
        rows = [SimpleNamespace(user_id=self.captain, username="captain at registration"),
                SimpleNamespace(user_id=self.member, username="member at registration")]
        db = Session(self.comp, None, team, rows, [])
        registration = await self.service.register_team(db, self.comp_id, self.team_id, self.captain)
        self.assertEqual(registration.team_name_snapshot, team.name)
        self.assertEqual(registration.registered_by, self.captain)
        self.assertIn("FROM competitions", sql(db.statements[0]))
        self.assertIn("FOR UPDATE", sql(db.statements[0]))
        team_lock = next(index for index, statement in enumerate(db.statements) if "FROM teams" in sql(statement))
        self.assertGreater(team_lock, 0)
        self.assertIn("FOR UPDATE", sql(db.statements[team_lock]))
        self.assertTrue(db.statements[0].get_execution_options()["populate_existing"])
        self.assertTrue(db.statements[team_lock].get_execution_options()["populate_existing"])
        members = [row for row in db.added if isinstance(row, models.CompetitionTeamMember)]
        self.assertEqual({(row.user_id, row.username_snapshot) for row in members},
            {(self.captain, rows[0].username), (self.member, rows[1].username)})
        self.assertFalse(any(isinstance(row, (models.CompetitionParticipant, models.CompetitionAssignment)) for row in db.added))
        self.assert_caller_transaction(db)

    async def test_noncaptain_new_registration_is_403_without_partial_rows(self):
        team = SimpleNamespace(id=self.team_id, captain_id=self.captain, name="team")
        db = Session(self.comp, None, team)
        with self.assertRaises(self.service.CompetitionTeamError) as caught:
            await self.service.register_team(db, self.comp_id, self.team_id, self.member)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(db.added, [])
        self.assert_caller_transaction(db)

    async def test_overlap_rejects_whole_roster_not_only_conflicting_member(self):
        team = SimpleNamespace(id=self.team_id, captain_id=self.captain, name="team")
        rows = [SimpleNamespace(user_id=self.captain, username="captain"), SimpleNamespace(user_id=self.member, username="member")]
        db = Session(self.comp, None, team, rows, [SimpleNamespace(user_id=self.member, team_id=uuid4())])
        with self.assertRaises(self.service.CompetitionTeamError) as caught:
            await self.service.register_team(db, self.comp_id, self.team_id, self.captain)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(db.added, [])
        self.assert_caller_transaction(db)

    async def test_invalid_roster_without_captain_cannot_be_partially_registered(self):
        team = SimpleNamespace(id=self.team_id, captain_id=self.captain, name="team")
        db = Session(self.comp, None, team, [SimpleNamespace(user_id=self.member, username="member")])
        with self.assertRaises(self.service.CompetitionTeamError) as caught:
            await self.service.register_team(db, self.comp_id, self.team_id, self.captain)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(db.added, [])

    async def test_same_team_retry_returns_original_without_live_roster_query(self):
        db = Session(self.comp, self.registration)
        result = await self.service.register_team(db, self.comp_id, self.team_id, self.captain)
        self.assertIs(result, self.registration)
        self.assertEqual(result.team_name_snapshot, "original team")
        self.assertEqual(len(db.statements), 2)
        self.assertFalse(any("FROM teams" in sql(statement) for statement in db.statements))
        self.assertEqual(db.added, [])
        self.assert_caller_transaction(db)

    async def test_existing_registration_does_not_grant_new_actor_registration_authority(self):
        db = Session(self.comp, self.registration)
        with self.assertRaises(self.service.CompetitionTeamError) as caught:
            await self.service.register_team(db, self.comp_id, self.team_id, uuid4())
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(db.added, [])

    async def test_unique_constraint_conflict_is_domain_409_and_caller_rolls_back(self):
        team = SimpleNamespace(id=self.team_id, captain_id=self.captain, name="team")
        rows = [SimpleNamespace(user_id=self.captain, username="captain")]
        error = IntegrityError("owned insert", {}, RuntimeError("duplicate roster"))
        db = Session(self.comp, None, team, rows, [], flush_error=error)
        with self.assertRaises(self.service.CompetitionTeamError) as caught:
            await self.service.register_team(db, self.comp_id, self.team_id, self.captain)
        self.assertEqual(caught.exception.status_code, 409)
        self.assert_caller_transaction(db)

    async def test_resolve_uses_fixed_snapshot_even_if_live_team_was_deleted(self):
        db = Session(self.comp, self.roster[1], self.registration, self.roster)
        context = await self.service.resolve_team_context(db, self.comp_id, self.member, lock=True)
        self.assertEqual((context.team_id, context.team_name), (self.team_id, "original team"))
        self.assertEqual(context.member.username_snapshot, "member at registration")
        self.assertEqual(tuple(context.members), tuple(self.roster))
        self.assertFalse(any("FROM teams " in sql(statement) or "JOIN team_members " in sql(statement) for statement in db.statements))
        locked = [sql(statement) for statement in db.statements if "FOR UPDATE" in sql(statement)]
        self.assertIn("FROM competitions", locked[0])
        self.assertIn("competition_team_registrations", locked[1])
        self.assert_caller_transaction(db)

    async def test_live_membership_without_fixed_roster_is_not_participation(self):
        db = Session(self.comp, None)
        self.assertIsNone(await self.service.resolve_team_context(db, self.comp_id, uuid4()))
        self.assertEqual(db.added, [])

    async def test_common_assignment_uniform_pool_max_five_and_not_difficulty_weights(self):
        db = Session(None, [11, 12, 13, 14, 15, 16, 17])
        sample = SimpleNamespace(sample=lambda pool, count: list(reversed(pool))[:count])
        with patch.object(self.service, "resolve_team_context", new=AsyncMock(return_value=self.context)) as resolve, \
                patch.object(self.service.secrets, "SystemRandom", return_value=sample):
            result = await self.service.get_or_create_team_assignment(db, self.comp_id, self.member, "web", [11, 12, 13, 14, 15, 16, 17])
        resolve.assert_awaited_once_with(db, self.comp_id, self.member, lock=True)
        self.assertFalse(result.already_assigned)
        self.assertEqual(result.assignment.challenge_ids, [17, 16, 15, 14, 13])
        self.assertEqual(result.assignment.team_id, self.team_id)
        self.assertFalse(any(isinstance(row, models.CompetitionAssignment) for row in db.added))
        self.assert_caller_transaction(db)

    async def test_assignment_retry_ignores_changed_pool_and_preserves_ids(self):
        assignment = model("CompetitionTeamAssignment", competition_id=self.comp_id, team_id=self.team_id,
            category="web", challenge_ids=[13, 11], assigned_at=NOW)
        db = Session(assignment)
        with patch.object(self.service, "resolve_team_context", new=AsyncMock(return_value=self.context)), \
                patch.object(self.service.secrets, "SystemRandom") as random:
            result = await self.service.get_or_create_team_assignment(db, self.comp_id, self.captain, "web", [])
        self.assertTrue(result.already_assigned)
        self.assertIs(result.assignment, assignment)
        random.assert_not_called()
        self.assertEqual(db.added, [])

    async def test_empty_current_public_ready_pool_does_not_create_assignment(self):
        db = Session(None, [])
        with patch.object(self.service, "resolve_team_context", new=AsyncMock(return_value=self.context)):
            with self.assertRaises(self.service.CompetitionTeamError) as caught:
                await self.service.get_or_create_team_assignment(db, self.comp_id, self.member, "web", [])
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(db.added, [])

    async def test_small_pool_is_sampled_without_replacement_and_scoped_to_category(self):
        db = Session(None, [11, 12])
        with patch.object(self.service, "resolve_team_context", new=AsyncMock(return_value=self.context)):
            result = await self.service.get_or_create_team_assignment(db, self.comp_id, self.member, "web", [11, 12, 99])
        self.assertEqual(set(result.assignment.challenge_ids), {11, 12})
        self.assertEqual(len(result.assignment.challenge_ids), 2)
        params = db.statements[1].compile(dialect=dialect()).params
        self.assertIn(self.comp_id, params.values())
        self.assertIn("web", params.values())
        self.assertIn([11, 12, 99], params.values())

    async def test_personal_and_team_solve_share_transaction_and_single_team_row(self):
        db = Session(11, Result(rowcount=1), None)
        with patch.object(self.service, "resolve_team_context", new=AsyncMock(return_value=self.context)):
            result = await self.service.record_team_solve(db, self.comp_id, self.member, 11, NOW)
        self.assertTrue(result.personal_inserted)
        self.assertTrue(result.team_inserted)
        self.assertFalse(result.earliest_changed)
        self.assertEqual((result.solve.first_solver_id, result.solve.solved_at), (self.member, NOW))
        self.assertIn("EXISTS", sql(db.statements[0]))
        self.assertIn("competition_team_assignments", sql(db.statements[0]))
        self.assertIn("ANY", sql(db.statements[0]))
        self.assertIn("INSERT INTO competition_solves", sql(db.statements[1]))
        self.assertIn("ON CONFLICT (competition_id, user_id, challenge_id) DO NOTHING", sql(db.statements[1]))
        self.assertEqual(sum(isinstance(row, models.CompetitionTeamSolve) for row in db.added), 1)
        self.assert_caller_transaction(db)

    async def test_second_member_personal_solve_does_not_increment_team_solve(self):
        earlier = model("CompetitionTeamSolve", competition_id=self.comp_id, team_id=self.team_id,
            challenge_id=11, first_solver_id=self.captain, solved_at=NOW)
        db = Session(11, Result(rowcount=1), earlier)
        with patch.object(self.service, "resolve_team_context", new=AsyncMock(return_value=self.context)):
            result = await self.service.record_team_solve(db, self.comp_id, self.member, 11, NOW + timedelta(seconds=1))
        self.assertTrue(result.personal_inserted)
        self.assertFalse(result.team_inserted or result.earliest_changed)
        self.assertEqual(db.added, [])
        self.assertEqual(earlier.first_solver_id, self.captain)
        self.assert_caller_transaction(db)

    async def test_earlier_accepted_reconciliation_only_corrects_team_time_and_first_solver(self):
        solve = model("CompetitionTeamSolve", competition_id=self.comp_id, team_id=self.team_id,
            challenge_id=11, first_solver_id=self.captain, solved_at=NOW)
        db = Session(11, Result(rowcount=0), solve)
        earlier = NOW - timedelta(seconds=3)
        with patch.object(self.service, "resolve_team_context", new=AsyncMock(return_value=self.context)):
            result = await self.service.record_team_solve(db, self.comp_id, self.member, 11, earlier)
        self.assertFalse(result.personal_inserted or result.team_inserted)
        self.assertTrue(result.earliest_changed)
        self.assertEqual((solve.solved_at, solve.first_solver_id), (earlier, self.member))
        self.assertEqual(db.added, [])
        self.assert_caller_transaction(db)

    async def test_equal_accepted_timestamp_keeps_existing_first_solver(self):
        solve = model("CompetitionTeamSolve", competition_id=self.comp_id, team_id=self.team_id,
            challenge_id=11, first_solver_id=self.captain, solved_at=NOW)
        db = Session(11, Result(rowcount=0), solve)
        with patch.object(self.service, "resolve_team_context", new=AsyncMock(return_value=self.context)):
            result = await self.service.record_team_solve(db, self.comp_id, self.member, 11, NOW)
        self.assertFalse(result.earliest_changed)
        self.assertEqual(solve.first_solver_id, self.captain)

    async def test_team_flush_failure_never_commits_personal_solve(self):
        db = Session(11, Result(rowcount=1), None, flush_error=RuntimeError("owned flush failure"))
        with patch.object(self.service, "resolve_team_context", new=AsyncMock(return_value=self.context)):
            with self.assertRaisesRegex(RuntimeError, "owned flush failure"):
                await self.service.record_team_solve(db, self.comp_id, self.member, 11, NOW)
        self.assert_caller_transaction(db)

    async def test_unassigned_or_removed_challenge_is_rejected_before_personal_insert(self):
        db = Session(None)
        with patch.object(self.service, "resolve_team_context", new=AsyncMock(return_value=self.context)):
            with self.assertRaises(self.service.CompetitionTeamError) as caught:
                await self.service.record_team_solve(db, self.comp_id, self.member, 99, NOW)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(len(db.statements), 1)
        self.assertEqual(db.added, [])
        self.assert_caller_transaction(db)

    async def test_missing_fixed_member_cannot_create_even_personal_solve(self):
        db = Session()
        with patch.object(self.service, "resolve_team_context", new=AsyncMock(return_value=None)):
            with self.assertRaises(self.service.CompetitionTeamError) as caught:
                await self.service.record_team_solve(db, self.comp_id, uuid4(), 11, NOW)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(db.statements, [])
        self.assertEqual(db.added, [])
        self.assert_caller_transaction(db)

    async def test_scoring_rows_keep_snapshot_names_and_strict_cutoff(self):
        solve = model("CompetitionTeamSolve", competition_id=self.comp_id, team_id=self.team_id,
            challenge_id=11, first_solver_id=self.captain, solved_at=NOW - timedelta(seconds=1))
        db = Session([self.registration], self.roster, [solve])
        rows = await self.service.load_team_scoring_rows(db, self.comp_id, cutoff=NOW)
        self.assertEqual(tuple(rows.registrations), (self.registration,))
        self.assertEqual(tuple(rows.members), tuple(self.roster))
        self.assertEqual(tuple(rows.solves), (solve,))
        self.assertIn("competition_team_solves.solved_at <", sql(db.statements[-1]))
        self.assertNotIn("<=", sql(db.statements[-1]))
        self.assertFalse(any("JOIN teams " in sql(statement) or "JOIN users " in sql(statement) for statement in db.statements))
        for statement in db.statements:
            self.assertIn(self.comp_id, statement.compile(dialect=dialect()).params.values())
        self.assert_caller_transaction(db)


class CompetitionTeamSourceTests(unittest.TestCase):
    def test_service_stays_caller_transaction_and_has_no_http_or_api_dependency(self):
        self.assertTrue(SERVICE.is_file(), "fixed-roster service is not implemented while source is frozen")
        tree = ast.parse(SERVICE.read_text(encoding="utf-8"), filename=str(SERVICE))
        forbidden = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                forbidden += [name for name in names if name.startswith(("httpx", "requests", "aiohttp", "app.api", "app.database"))]
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                if node.func.value.id == "db" and node.func.attr in {"commit", "rollback", "begin", "close"}:
                    forbidden.append("db." + node.func.attr)
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
