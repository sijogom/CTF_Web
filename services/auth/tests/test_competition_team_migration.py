"""Metadata and additive migration contracts; these tests do not connect to PG."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import CheckConstraint, UniqueConstraint
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import dialect

from app.models import competition as models
from app.utils.auth_schema_bootstrap_contract import CURRENT_AUTH_REQUIRED_COLUMNS, LEGACY_CORE_TABLES


ROOT = Path(__file__).parents[1]
MIGRATION = ROOT / "migrations/versions/20261018_006_competition_teams.py"
SCHEMA = "owned_team_contract_unit"
TEAM_TABLES = {"competition_team_registrations", "competition_team_members", "competition_team_assignments", "competition_team_solves"}


class Scalar:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalar(self):
        return self.value


class Bind:
    def __init__(self, *, has_rows=False, schema=SCHEMA):
        self.dialect = dialect()
        self.dialect.default_schema_name = schema
        self.schema = schema
        self.has_rows, self.statements = has_rows, []

    def execute(self, statement, *args, **kwargs):
        text = str(statement)
        self.statements.append(text)
        if "current_schema()" in text:
            return Scalar(self.schema)
        return Scalar(self.has_rows)

    exec_driver_sql = execute


class Operations:
    def __init__(self, bind):
        self.bind, self.calls = bind, []

    def get_bind(self):
        return self.bind

    def execute(self, statement, *args, **kwargs):
        return self.bind.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        if name not in {"add_column", "create_table", "create_index", "create_check_constraint", "create_foreign_key",
                        "drop_table", "drop_column", "drop_index", "drop_constraint"}:
            raise AssertionError("unexpected Alembic operation: " + name)
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record


def migration():
    if not MIGRATION.is_file():
        raise AssertionError("team migration is not implemented while source is frozen")
    spec = importlib.util.spec_from_file_location("owned_competition_team_migration", MIGRATION)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def table(name):
    cls = getattr(models, name, None)
    if cls is None:
        raise AssertionError("missing team model: " + name)
    return cls.__table__


class CompetitionTeamMetadataTests(unittest.TestCase):
    def test_nullable_marker_keeps_legacy_null_and_restricts_fixed_contract_to_team(self):
        columns = models.Competition.__table__.c
        self.assertIn("team_contract", columns)
        self.assertTrue(columns.team_contract.nullable)
        self.assertIsNone(columns.team_contract.default)
        self.assertIsNone(columns.team_contract.server_default)
        checks = " ".join(str(item.sqltext) for item in models.Competition.__table__.constraints if isinstance(item, CheckConstraint))
        self.assertIn("team_contract IS NULL", checks)
        self.assertIn("fixed_roster_v1", checks)
        self.assertIn("mode", checks)
        self.assertIn("team", checks)

    def test_registration_keeps_team_identity_without_live_team_cascade(self):
        registration = table("CompetitionTeamRegistration")
        self.assertEqual(tuple(registration.primary_key.columns.keys()), ("competition_id", "team_id"))
        self.assertTrue({"registered_by", "registered_at", "team_name_snapshot"} <= set(registration.c.keys()))
        self.assertEqual(set(registration.c.team_id.foreign_keys), set())
        self.assertEqual({fk.ondelete for fk in registration.c.registered_by.foreign_keys}, {None})

    def test_roster_uniqueness_is_competition_user_not_only_team_user(self):
        roster = table("CompetitionTeamMember")
        unique_sets = {tuple(item.columns.keys()) for item in roster.constraints if isinstance(item, UniqueConstraint)}
        self.assertIn(("competition_id", "user_id"), unique_sets)
        self.assertIn("username_snapshot", roster.c)
        self.assertEqual({fk.ondelete for fk in roster.c.user_id.foreign_keys if fk.target_fullname == "users.id"}, {None})
        targets = {element.target_fullname for item in roster.foreign_key_constraints for element in item.elements}
        self.assertTrue({"competition_team_registrations.competition_id", "competition_team_registrations.team_id"} <= targets)

    def test_assignment_and_team_solve_are_once_per_team(self):
        assignment = table("CompetitionTeamAssignment")
        solve = table("CompetitionTeamSolve")
        self.assertEqual(tuple(assignment.primary_key.columns.keys()), ("competition_id", "team_id", "category"))
        self.assertEqual(tuple(solve.primary_key.columns.keys()), ("competition_id", "team_id", "challenge_id"))
        self.assertTrue({"first_solver_id", "solved_at"} <= set(solve.c.keys()))
        self.assertTrue(solve.c.solved_at.type.timezone)
        for current in (assignment, solve):
            self.assertFalse(any(element.target_fullname.startswith("teams.") for item in current.foreign_key_constraints for element in item.elements))

    def test_bootstrap_requires_new_head_without_changing_historical_core(self):
        self.assertTrue(TEAM_TABLES <= set(CURRENT_AUTH_REQUIRED_COLUMNS))
        self.assertTrue(TEAM_TABLES.isdisjoint(LEGACY_CORE_TABLES))
        self.assertIn("team_contract", CURRENT_AUTH_REQUIRED_COLUMNS["competitions"])
        env = (ROOT / "migrations/env.py").read_text(encoding="utf-8")
        for name in ("CompetitionTeamRegistration", "CompetitionTeamMember", "CompetitionTeamAssignment", "CompetitionTeamSolve"):
            self.assertIn(name, env)


class CompetitionTeamMigrationTests(unittest.TestCase):
    def test_revision_is_additive_after_005(self):
        module = migration()
        self.assertEqual((module.revision, module.down_revision), ("20261018_006", "20261013_005"))

    def test_upgrade_only_adds_marker_and_new_ledgers_in_target_schema(self):
        module, bind = migration(), Bind()
        operations = Operations(bind)
        inspector = SimpleNamespace(get_columns=lambda *args, **kwargs: [])
        with patch.object(module, "op", operations), patch.object(module.sa, "inspect", return_value=inspector):
            module.upgrade()
        tables = [args[0] for name, args, kwargs in operations.calls if name == "create_table"]
        self.assertEqual(set(tables), TEAM_TABLES)
        marker = [(args, kwargs) for name, args, kwargs in operations.calls if name == "add_column"]
        self.assertEqual(len(marker), 1)
        self.assertEqual((marker[0][0][0], marker[0][0][1].name), ("competitions", "team_contract"))
        self.assertTrue(marker[0][0][1].nullable)
        self.assertIsNone(marker[0][0][1].server_default)
        for name, args, kwargs in operations.calls:
            if name in {"create_table", "add_column"}:
                self.assertEqual(kwargs.get("schema"), SCHEMA)
            self.assertNotIn(name, {"drop_table", "drop_column"})
        self.assertFalse(any(statement.lstrip().upper().startswith("UPDATE ") for statement in bind.statements))

    def test_legacy_assignment_and_participant_write_guards_are_schema_bound_invokers(self):
        module, bind = migration(), Bind()
        operations = Operations(bind)
        inspector = SimpleNamespace(get_columns=lambda *args, **kwargs: [])
        with patch.object(module, "op", operations), patch.object(module.sa, "inspect", return_value=inspector):
            module.upgrade()
        source = "\n".join(bind.statements)
        upper = source.upper()
        self.assertIn("SECURITY INVOKER", upper)
        self.assertNotIn("SECURITY DEFINER", upper)
        for target in ("competition_participants", "competition_assignments"):
            self.assertIn(target, source)
        self.assertIn("fixed_roster_v1", source)
        self.assertIn(SCHEMA, source)
        self.assertNotIn("public.", source)
        self.assertNotIn("set_config", source.lower())
        self.assertIn("BEFORE INSERT OR UPDATE", upper)
        self.assertIn("OLD.competition_id", source)
        self.assertIn("NEW.competition_id", source)
        self.assertIn("ORDER BY id FOR SHARE", source)

    def test_upgrade_foreign_keys_are_only_in_the_validated_target_schema(self):
        module, bind = migration(), Bind()
        operations = Operations(bind)
        inspector = SimpleNamespace(get_columns=lambda *args, **kwargs: [])
        with patch.object(module, "op", operations), patch.object(module.sa, "inspect", return_value=inspector):
            module.upgrade()
        for name, args, kwargs in operations.calls:
            if name != "create_table":
                continue
            for item in args[1:]:
                foreign_keys = getattr(item, "foreign_keys", ())
                targets = [key.target_fullname for key in foreign_keys]
                if hasattr(item, "elements"):
                    targets += [key.target_fullname for key in item.elements]
                for target in targets:
                    self.assertTrue(target.startswith(SCHEMA + "."), target)
                    self.assertNotIn(".teams.", target)

    def test_fresh_bootstrap_metadata_marker_is_validated_not_added_twice(self):
        module, bind = migration(), Bind()
        operations = Operations(bind)
        inspector = SimpleNamespace(
            get_columns=lambda *args, **kwargs: [{"name": "team_contract", "type": sa.String(30), "nullable": True, "default": None}],
            get_check_constraints=lambda *args, **kwargs: [{"name": "ck_competitions_team_contract"}],
        )
        with patch.object(module, "op", operations), patch.object(module.sa, "inspect", return_value=inspector):
            module.upgrade()
        self.assertFalse(any(name == "add_column" for name, args, kwargs in operations.calls))
        checks = [(name, args) for name, args, kwargs in operations.calls if name in {"drop_constraint", "create_check_constraint"}]
        self.assertEqual([name for name, args in checks], ["drop_constraint", "create_check_constraint"])
        self.assertEqual(checks[0][1], ("ck_competitions_team_contract", "competitions"))

    def test_existing_incompatible_marker_refuses_before_any_ddl(self):
        for overrides in ({"type": sa.Integer()}, {"type": sa.String(20)}, {"nullable": False}, {"default": "'fixed_roster_v1'"}):
            module, bind = migration(), Bind()
            operations = Operations(bind)
            column = {"name": "team_contract", "type": sa.String(30), "nullable": True, "default": None, **overrides}
            inspector = SimpleNamespace(get_columns=lambda *args, **kwargs: [column])
            with self.subTest(overrides=overrides), patch.object(module, "op", operations), patch.object(module.sa, "inspect", return_value=inspector):
                with self.assertRaises(RuntimeError):
                    module.upgrade()
            self.assertEqual(operations.calls, [])

    def test_unresolved_or_unsafe_schema_refuses_before_any_ddl(self):
        for schema in (None, "", "owned; DROP TABLE users", "other.schema", 'bad"schema'):
            module, bind = migration(), Bind(schema=schema)
            operations = Operations(bind)
            with self.subTest(schema=schema), patch.object(module, "op", operations):
                with self.assertRaises(RuntimeError):
                    module.upgrade()
                self.assertEqual(operations.calls, [])

    def test_destructive_downgrade_locks_before_refusing_any_new_data(self):
        module, bind = migration(), Bind(has_rows=True)
        operations = Operations(bind)
        with patch.object(module, "op", operations):
            with self.assertRaises(RuntimeError):
                module.downgrade()
        mutation = [name for name, args, kwargs in operations.calls if name.startswith("drop_")]
        self.assertEqual(mutation, [])
        lock = next(index for index, text in enumerate(bind.statements) if "ACCESS EXCLUSIVE" in text.upper())
        checks = [index for index, text in enumerate(bind.statements) if "EXISTS" in text.upper()]
        self.assertTrue(checks)
        self.assertLess(lock, min(checks))

    def test_empty_downgrade_removes_only_new_schema_objects_after_locked_check(self):
        module, bind = migration(), Bind()
        operations = Operations(bind)
        with patch.object(module, "op", operations):
            module.downgrade()
        dropped = [args[0] for name, args, kwargs in operations.calls if name == "drop_table"]
        self.assertEqual(set(dropped), TEAM_TABLES)
        self.assertEqual(next(args for name, args, kwargs in operations.calls if name == "drop_column"),
                         ("competitions", "team_contract"))
        source = "\n".join(bind.statements)
        self.assertIn("WHERE team_contract IS NOT NULL", source)
        for name, args, kwargs in operations.calls:
            self.assertEqual(kwargs["schema"], SCHEMA)

    def test_migration_has_no_runtime_http_config_or_public_schema_fallback(self):
        self.assertTrue(MIGRATION.is_file(), "team migration is not implemented while source is frozen")
        source = MIGRATION.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MIGRATION))
        modules = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any(name.startswith(("app.config", "app.database", "app.api", "httpx", "requests")) for name in modules))
        self.assertNotIn("SECURITY DEFINER", source.upper())
        self.assertNotIn("set_config", source)


if __name__ == "__main__":
    unittest.main()
