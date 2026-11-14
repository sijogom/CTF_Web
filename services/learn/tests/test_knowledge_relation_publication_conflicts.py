"""legacy 후보와 제안서 공개의 충돌만 검증하는 독립 회귀 검사."""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from app.api import knowledge_relations as api
from app.models.course import Base
import app.models.education
import app.models.event
import app.models.challenge_submission
import app.models.lesson_image
import app.models.challenge_image_cleanup
import app.models.challenge_attachment
from app.models.challenge import Challenge
from app.models.knowledge import (
    KnowledgeGraphRevision, KnowledgeProjectionOutbox, KnowledgeRelation,
    KnowledgeRelationProposal, KnowledgeRelationProposalReview, KnowledgeRelationReview,
)
from app.schemas.knowledge import KnowledgeProposalReviewRequest, KnowledgeRelationCreate, KnowledgeRelationProposalCreate


CONSTRAINT = "uq_knowledge_relation_active_pair"
DATABASE_URL = os.environ.get("KNOWLEDGE_TEST_DATABASE_URL")


class DriverError(Exception):
    def __init__(self, constraint_name=None, sqlstate=None):
        super().__init__("driver error")
        self.constraint_name = constraint_name
        self.sqlstate = sqlstate


class PublicationConstraintTests(unittest.TestCase):
    def test_asyncpg_explicit_cause_uses_exact_constraint_name(self):
        adapter = DriverError(sqlstate="23505")
        adapter.__cause__ = DriverError(CONSTRAINT, "23505")
        error = IntegrityError("insert", {}, adapter)
        self.assertTrue(api._is_expected_unique_constraint_violation(error, CONSTRAINT))
        self.assertFalse(api._is_expected_unique_constraint_violation(error, CONSTRAINT + "_other"))

    def test_psycopg_diagnostic_remains_supported(self):
        original = DriverError(sqlstate="23505")
        original.diag = SimpleNamespace(constraint_name=CONSTRAINT)
        self.assertTrue(api._is_expected_unique_constraint_violation(IntegrityError("insert", {}, original), CONSTRAINT))

    def test_other_integrity_states_and_implicit_context_are_not_duplicates(self):
        for state in ("23503", "23514", "23502"):
            original = DriverError(CONSTRAINT, state)
            self.assertFalse(api._is_expected_unique_constraint_violation(IntegrityError("insert", {}, original), CONSTRAINT))
        original = DriverError(sqlstate="23505")
        original.__context__ = DriverError(CONSTRAINT, "23505")
        self.assertFalse(api._is_expected_unique_constraint_violation(IntegrityError("insert", {}, original), CONSTRAINT))

    def test_cyclic_or_unlabelled_cause_is_bounded_and_rejected(self):
        original = DriverError(sqlstate="23505")
        original.__cause__ = original
        self.assertFalse(api._is_expected_unique_constraint_violation(IntegrityError("insert", {}, original), CONSTRAINT))
        self.assertFalse(api._is_expected_unique_constraint_violation(IntegrityError("insert", {}, Exception("unique " + CONSTRAINT)), CONSTRAINT))


@unittest.skipUnless(DATABASE_URL, "명시된 KNOWLEDGE_TEST_DATABASE_URL 작업 DB가 필요합니다.")
class PublicationConflictPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "knowledge_conflict_" + uuid4().hex
        self.admin_engine = create_async_engine(DATABASE_URL)
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(DATABASE_URL, connect_args={"server_settings": {
            "search_path": self.schema, "statement_timeout": "10000"}}, pool_size=4)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            def install(sync):
                path = SERVICE / "migrations/versions/20261013_028_knowledge_mapping_ownership_revision.py"
                spec = importlib.util.spec_from_file_location("publication_conflict_revision", path)
                migration = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(migration)
                with Operations.context(MigrationContext.configure(sync)):
                    migration.upgrade()
            await connection.run_sync(install)
        self.author, self.other_author, self.reviewer = uuid4(), uuid4(), uuid4()
        async with self.session() as db:
            rows = [Challenge(name="충돌 원본", category="web", challenge_type="wargame", is_published=True),
                    Challenge(name="충돌 대상", category="web", challenge_type="wargame", is_published=True)]
            db.add_all(rows)
            await db.commit()
            self.source, self.target = [row.id for row in rows]
        async with self.session() as db:
            proposal = await api.create_knowledge_proposal(KnowledgeRelationProposalCreate(
                source={"type": "challenge", "id": str(self.source)}, target={"type": "challenge", "id": str(self.target)},
                relation_type="helps", evidence_summary="제안 검토 근거", evidence=[{"evidence_kind": "manual", "summary": "기존 원본 보존"}]),
                db=db, actor_id=self.author, _role="admin")
            self.proposal_id = proposal.id

    async def asyncTearDown(self):
        await self.engine.dispose()
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.admin_engine.dispose()

    def session(self):
        return AsyncSession(self.engine, expire_on_commit=False)

    async def create_legacy(self):
        async with self.session() as db:
            return await api.create_knowledge_relation(KnowledgeRelationCreate(
                source={"type": "challenge", "id": str(self.source)}, target={"type": "challenge", "id": str(self.target)},
                relation_type="helps", evidence_summary="기존 후보 근거"), db=db, actor_id=self.other_author, _role="admin")

    async def snapshot(self):
        async with self.session() as db:
            proposal = await db.get(KnowledgeRelationProposal, self.proposal_id)
            relations = (await db.scalars(select(KnowledgeRelation).order_by(KnowledgeRelation.id))).all()
            return {
                "proposal": api._proposal_snapshot(proposal),
                "relations": [(str(row.id), api._relation_snapshot(row)) for row in relations],
                "relation_reviews": await db.scalar(select(func.count()).select_from(KnowledgeRelationReview)),
                "proposal_reviews": await db.scalar(select(func.count()).select_from(KnowledgeRelationProposalReview)),
                "outbox": await db.scalar(select(func.count()).select_from(KnowledgeProjectionOutbox)),
                "revisions": await db.scalar(select(func.count()).select_from(KnowledgeGraphRevision)),
            }

    def assert_actionable_conflict(self, error):
        self.assertEqual(error.status_code, 409)
        self.assertIn("관계 목록", error.detail)
        self.assertIn("승인·반려", error.detail)
        self.assertIn("개정", error.detail)
        self.assertNotIn("무결성 오류", error.detail)

    async def test_existing_legacy_pending_conflict_is_actionable_and_changes_nothing(self):
        legacy = await self.create_legacy()
        before = await self.snapshot()
        async with self.session() as db:
            with self.assertRaises(HTTPException) as raised:
                await api.review_knowledge_proposal(self.proposal_id,
                    KnowledgeProposalReviewRequest(action="approve", reason="독립 검토"), db=db, actor_id=self.reviewer, _role="admin")
            self.assert_actionable_conflict(raised.exception)
            await db.rollback()
        after = await self.snapshot()
        self.assertEqual(before, after)
        self.assertEqual(after["relations"][0][0], str(legacy.id))
        self.assertEqual(after["relations"][0][1]["status"], "pending_review")
        self.assertEqual(after["proposal"]["state"], "pending_review")

    async def test_concurrent_legacy_insert_after_precheck_is_actionable_and_rolls_back(self):
        passed_precheck = asyncio.Event()
        competitor_committed = asyncio.Event()
        original = api._assert_relation_not_duplicated
        async with self.session() as approving:
            async def pause_after_precheck(db, **kwargs):
                await original(db, **kwargs)
                if db is approving:
                    passed_precheck.set()
                    await asyncio.wait_for(competitor_committed.wait(), 5)
            async def approve():
                try:
                    await api.review_knowledge_proposal(self.proposal_id,
                        KnowledgeProposalReviewRequest(action="approve", reason="동시 검토"), db=approving, actor_id=self.reviewer, _role="admin")
                except HTTPException as error:
                    return error
                return None
            with patch.object(api, "_assert_relation_not_duplicated", side_effect=pause_after_precheck):
                pending = asyncio.create_task(approve())
                try:
                    await asyncio.wait_for(passed_precheck.wait(), 5)
                    await self.create_legacy()
                    expected = await self.snapshot()
                    competitor_committed.set()
                    error = await asyncio.wait_for(pending, 5)
                    self.assertIsNotNone(error)
                    self.assert_actionable_conflict(error)
                finally:
                    competitor_committed.set()
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(expected, await self.snapshot())


if __name__ == "__main__":
    unittest.main()
