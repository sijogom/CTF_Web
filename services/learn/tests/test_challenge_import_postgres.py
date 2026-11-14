"""작업 PostgreSQL16의 새 UUID schema에 한정한 실제 DB 회귀 테스트.

이 파일은 명시적인 CHALLENGE_IMPORT_TEST_DATABASE_URL 없이는 접속하지 않는다.
운영 DB, 기존 public row, 객체 저장소와 외부 Auth에는 쓰지 않는다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.course import Base, Course, Lesson
import app.models.event
import app.models.challenge_submission
import app.models.lesson_image
import app.models.challenge_image_cleanup
from app.models.challenge import (
    Challenge, ChallengeFlagPolicy, ChallengePrerequisite, DynamicFlagIssuance,
    Flag, Hint, HintUnlock, Submission,
)
from app.models.education import EducationResourceLink, TeachingClass
from app.models.knowledge import (
    ChallengeKnowledgeTag, ChallengeLearningPrerequisite, ChallengeRecommendedPrerequisite,
    KnowledgeContentNode, KnowledgeTag, KnowledgeTagMappingAuditEvent,
)
from app.services.challenge_imports import IMPORT_LOCK_KEY, export_challenge_batch, import_challenge_batch
from app.utils.environment_verification import next_environment_revision


DATABASE_URL = os.environ.get("CHALLENGE_IMPORT_TEST_DATABASE_URL")


def validate_work_database_url(value):
    url = make_url(value)
    if (url.query or url.drivername != "postgresql+asyncpg" or url.host != "127.0.0.1" or url.port != 15433
            or not re.fullmatch(r"ctfi_l_[0-9a-f]{32}", url.database or "") or url.username != url.database):
        raise ValueError("새 작업 소유 ctfi_l UUID DB와 같은 이름의 전용 role만 허용합니다.")
    return url


def invalidate_environment(challenge):
    challenge.environment_revision = next_environment_revision(challenge.environment_revision)
    challenge.environment_verified_revision = None
    challenge.environment_verified_at = None
    challenge.environment_verified_by = None
    challenge.environment_verified_image_id = None
    challenge.environment_last_failure_code = "configuration_changed"


async def revoke_issuances(db, *, challenge_id):
    await db.execute(update(DynamicFlagIssuance).where(
        DynamicFlagIssuance.challenge_id == challenge_id,
        DynamicFlagIssuance.revoked_at.is_(None), DynamicFlagIssuance.consumed_at.is_(None),
    ).values(revoked_at=datetime.now(timezone.utc), revocation_reason="environment_stale"))


@unittest.skipUnless(DATABASE_URL, "명시적인 작업 전용 CHALLENGE_IMPORT_TEST_DATABASE_URL이 필요합니다.")
class ChallengeImportPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = validate_work_database_url(DATABASE_URL)
        self.schemas = []
        self.engines = []
        self.admin_engine = create_async_engine(DATABASE_URL)
        self.addAsyncCleanup(self.cleanup_owned_schemas)
        async with self.admin_engine.connect() as connection:
            identity = (await connection.execute(text("SELECT current_database(),current_user,current_setting('server_version_num')::int"))).one()
            self.assertEqual(tuple(identity[:2]), (url.database, url.username))
            self.assertTrue(160000 <= identity[2] < 170000)
        self.engine = await self.new_schema()
        self.actor = uuid4()

    async def new_schema(self):
        schema = "challenge_import_test_" + uuid4().hex
        async with self.admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        self.schemas.append(schema)
        engine = create_async_engine(DATABASE_URL, connect_args={"server_settings": {"search_path": schema}}, pool_size=5)
        self.engines.append(engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        return engine

    async def cleanup_owned_schemas(self):
        try:
            for engine in self.engines:
                await engine.dispose()
            if self.schemas:
                async with self.admin_engine.begin() as connection:
                    for schema in self.schemas:
                        self.assertRegex(schema, r"^challenge_import_test_[0-9a-f]{32}$")
                        await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await self.admin_engine.dispose()

    def session(self, engine=None):
        return AsyncSession(engine or self.engine, expire_on_commit=False)

    async def import_data(self, db, rows, *, mode="overwrite", version=2):
        return await import_challenge_batch(db, {"version": version, "mode": mode, "challenges": rows},
            actor_id=self.actor, validate_dynamic_policy=lambda _: None,
            invalidate_environment=invalidate_environment, revoke_active_issuances=revoke_issuances)

    async def seed(self):
        async with self.session() as db:
            challenge = Challenge(name="Existing", category="web", description="<p>Original</p>",
                                  flag_value="synthetic-original", value=0, xp=0, solves=7,
                                  author_id=self.actor, is_published=True)
            db.add(challenge)
            await db.flush()
            hint = Hint(challenge_id=challenge.id, content="<p>Original hint</p>", cost=0, order_index=0)
            flag = Flag(challenge_id=challenge.id, value="synthetic-original", type="static", order_index=0)
            tag = KnowledgeTag(name="Manual-only", slug="manual-only")
            imported_tag = KnowledgeTag(name="Import-only", slug="import-only")
            course = Course(title="Retained course", category="web", difficulty="basic", author_id=self.actor)
            teaching = TeachingClass(title="Retained class", instructor_id=self.actor, created_by=self.actor, updated_by=self.actor)
            db.add_all([hint, flag, tag, imported_tag, course, teaching])
            await db.flush()
            lesson = Lesson(course_id=course.id, title="Retained lesson", content="<p>Lesson</p>", challenge_id=challenge.id)
            submission = Submission(user_id=self.actor, challenge_id=challenge.id, submitted_flag="[test]", is_correct=True)
            unlock = HintUnlock(user_id=self.actor, hint_id=hint.id)
            mapping = ChallengeKnowledgeTag(challenge_id=challenge.id, tag_id=tag.id, origin="manual",
                                            strength=0.2, confidence=0.4, evidence_summary="Keep manual", created_by=self.actor)
            imported_mapping = ChallengeKnowledgeTag(challenge_id=challenge.id, tag_id=imported_tag.id, origin="import",
                                            strength=0.6, confidence=0.8, evidence_summary="Keep imported", created_by=self.actor)
            resource = EducationResourceLink(class_id=teaching.id, resource_type="wargame", challenge_id=challenge.id, created_by=self.actor)
            learning = ChallengeLearningPrerequisite(challenge_id=challenge.id, prerequisite_type="course", prerequisite_target_id=course.id)
            recommended = ChallengeRecommendedPrerequisite(challenge_id=challenge.id, rec_type="course", rec_target_id=str(course.id))
            node = KnowledgeContentNode(entity_type="challenge", entity_id=str(challenge.id), display_name=challenge.name)
            db.add_all([lesson, submission, unlock, mapping, imported_mapping, resource, learning, recommended, node])
            await db.commit()
            return {"challenge": challenge.id, "hint": hint.id, "flag": flag.id, "submission": submission.id,
                    "unlock": unlock.id, "tag": tag.id, "imported_tag": imported_tag.id,
                    "lesson": lesson.id, "resource": resource.id, "learning": learning.id,
                    "recommended": recommended.id, "node": node.id,
                    "created_at": challenge.created_at, "author_id": challenge.author_id}

    async def test_overwrite_keeps_challenge_child_history_and_real_education_references(self):
        identifiers = await self.seed()
        async with self.session() as db:
            result = await self.import_data(db, [{"id": 99999, "name": "Existing", "description": "Changed",
                "hints": [{"id": 9191, "content": "Changed hint", "order_index": 0}],
                "flags": [{"id": 8181, "value": "synthetic-changed", "order_index": 0}]}])
            self.assertEqual(result, {"created": 0, "updated": 1, "skipped": 0})
        async with self.session() as db:
            challenge = await db.get(Challenge, identifiers["challenge"])
            self.assertEqual((challenge.description, challenge.solves, challenge.author_id, challenge.created_at),
                             ("<p>Changed</p>", 7, identifiers["author_id"], identifiers["created_at"]))
            self.assertEqual((await db.get(Hint, identifiers["hint"])).content, "<p>Changed hint</p>")
            self.assertEqual((await db.get(Flag, identifiers["flag"])).value, "synthetic-changed")
            for model, key in ((Submission, "submission"), (HintUnlock, "unlock"), (Lesson, "lesson"),
                               (EducationResourceLink, "resource"), (ChallengeLearningPrerequisite, "learning"),
                               (ChallengeRecommendedPrerequisite, "recommended"), (KnowledgeContentNode, "node")):
                self.assertIsNotNone(await db.get(model, identifiers[key]))
            for key, origin in (("tag", "manual"), ("imported_tag", "import")):
                row = await db.get(ChallengeKnowledgeTag, (identifiers["challenge"], identifiers[key]))
                self.assertEqual(row.origin, origin)

    async def test_round_trip_is_idempotent_and_skip_is_byte_equivalent_for_existing_rows(self):
        identifiers = await self.seed()
        async with self.session() as db:
            exported = await export_challenge_batch(db)
            self.assertEqual(exported["version"], 2)
        async with self.session() as db:
            self.assertEqual(await self.import_data(db, exported["challenges"]), {"created": 0, "updated": 1, "skipped": 0})
        async with self.session() as db:
            before = await export_challenge_batch(db)
        self.assertEqual(before, exported)
        modified = before["challenges"][0] | {"description": "Skip must not write", "hints": [], "flags": [], "category": "crypto"}
        async with self.session() as db:
            self.assertEqual(await self.import_data(db, [modified], mode="skip"), {"created": 0, "updated": 0, "skipped": 1})
        async with self.session() as db:
            self.assertEqual(await export_challenge_batch(db), before)
            self.assertEqual(await db.scalar(select(func.count()).select_from(KnowledgeTagMappingAuditEvent)), 0)
            self.assertIsNotNone(await db.get(HintUnlock, identifiers["unlock"]))

    async def test_distinct_schema_source_id_mapping_and_skip_reference_mapping(self):
        source_engine = await self.new_schema()
        async with self.session(source_engine) as db:
            first = Challenge(id=700, name="Source A", category="web", is_published=True, flag_value="synthetic-a")
            second = Challenge(id=701, name="Source B", category="web", is_published=True, flag_value="synthetic-b")
            db.add_all([first, second])
            await db.flush()
            db.add(ChallengePrerequisite(challenge_id=701, prerequisite_id=700))
            await db.commit()
            exported = await export_challenge_batch(db)
        async with self.session() as db:
            db.add(Challenge(id=700, name="Unrelated collision", category="web", is_published=True))
            await db.commit()
        async with self.session() as db:
            result = await self.import_data(db, exported["challenges"])
            self.assertEqual(result["created"], 2)
        async with self.session() as db:
            ids = dict((await db.execute(select(Challenge.name, Challenge.id))).all())
            self.assertNotEqual(ids["Source A"], 700)
            self.assertEqual(set((await db.execute(select(ChallengePrerequisite.challenge_id, ChallengePrerequisite.prerequisite_id))).all()),
                             {(ids["Source B"], ids["Source A"])})
        async with self.session() as db:
            result = await self.import_data(db, [{"id": 700, "name": "Source A"},
                {"id": 999, "name": "New C", "prerequisite_ids": [700]}], mode="skip")
            self.assertEqual(result, {"created": 1, "updated": 0, "skipped": 1})
        async with self.session() as db:
            third = await db.scalar(select(Challenge.id).where(Challenge.name == "New C"))
            self.assertIsNotNone(await db.get(ChallengePrerequisite, (third, ids["Source A"])))

    async def test_late_projection_failure_rolls_back_every_parent_child_and_edge(self):
        identifiers = await self.seed()
        async with self.session() as db:
            before = await export_challenge_batch(db)
        async with self.session() as db:
            with patch("app.services.challenge_imports.project_rule_tag_mappings", new=AsyncMock(side_effect=RuntimeError("injected projection failure"))):
                with self.assertRaisesRegex(RuntimeError, "injected projection failure"):
                    await self.import_data(db, [
                        {"id": 1, "name": "Existing", "description": "Must roll back", "hints": [{"content": "Must roll back"}]},
                        {"id": 2, "name": "Must not remain", "prerequisite_ids": [1]},
                    ])
        async with self.session() as db:
            self.assertEqual(await export_challenge_batch(db), before)
            self.assertIsNotNone(await db.get(HintUnlock, identifiers["unlock"]))
            self.assertEqual(await db.scalar(select(func.count()).select_from(ChallengePrerequisite)), 0)

    async def test_deferred_commit_failure_rolls_back_full_batch(self):
        await self.seed()
        async with self.engine.begin() as connection:
            await connection.execute(text("CREATE TABLE import_commit_guard (id int PRIMARY KEY, target int REFERENCES challenges(id) DEFERRABLE INITIALLY DEFERRED)"))
        async with self.session() as db:
            before = await export_challenge_batch(db)
        async def add_invalid_deferred_reference(db, **_kwargs):
            await db.execute(text("INSERT INTO import_commit_guard (id,target) VALUES (1,2147483647)"))
        async with self.session() as db:
            with patch("app.services.challenge_imports.project_rule_tag_mappings", new=add_invalid_deferred_reference):
                with self.assertRaises(HTTPException) as caught:
                    await self.import_data(db, [{"id": 1, "name": "Existing", "description": "Must not commit"}])
                self.assertEqual(caught.exception.detail["code"], "import_storage_rejected")
        async with self.session() as db:
            self.assertEqual(await export_challenge_batch(db), before)
            self.assertEqual(await db.scalar(text("SELECT count(*) FROM import_commit_guard")), 0)

    async def test_busy_import_advisory_lock_is_conflict_then_releases(self):
        async with self.session() as holder:
            await holder.scalar(select(func.pg_advisory_xact_lock(IMPORT_LOCK_KEY)))
            async with self.session() as contender:
                with self.assertRaises(HTTPException) as caught:
                    await asyncio.wait_for(self.import_data(contender, [{"id": 1, "name": "A"}]), timeout=3)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(caught.exception.detail["code"], "import_in_progress")
            await holder.rollback()
        async with self.session() as db:
            self.assertEqual((await self.import_data(db, [{"id": 1, "name": "A"}]))["created"], 1)

    async def test_real_parent_row_lock_conflicts_without_partial_writes(self):
        identifiers = await self.seed()
        async with self.session() as holder:
            await holder.execute(select(Challenge).where(Challenge.id == identifiers["challenge"]).with_for_update())
            async with self.session() as contender:
                with self.assertRaises(HTTPException) as caught:
                    await asyncio.wait_for(self.import_data(contender, [{"id": 1, "name": "Existing", "description": "Blocked"}, {"id": 2, "name": "No partial"}]), timeout=3)
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(caught.exception.detail["code"], "import_conflict")
            await holder.rollback()
        async with self.session() as db:
            self.assertEqual((await db.get(Challenge, identifiers["challenge"])).description, "<p>Original</p>")
            self.assertEqual(await db.scalar(select(func.count()).select_from(Challenge)), 1)

    async def test_two_actual_overwrites_cannot_apply_simultaneously(self):
        await self.seed()
        entered = asyncio.Event()
        release = asyncio.Event()
        async def hold_first_projection(*_args, **_kwargs):
            entered.set()
            await release.wait()
        async with self.session() as first, self.session() as second:
            with patch("app.services.challenge_imports.project_rule_tag_mappings", new=hold_first_projection):
                first_task = asyncio.create_task(self.import_data(first, [{"id": 1, "name": "Existing", "description": "First writer"}]))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    with self.assertRaises(HTTPException) as caught:
                        await asyncio.wait_for(self.import_data(second, [{"id": 2, "name": "Existing", "description": "Second writer"}]), timeout=3)
                    self.assertEqual(caught.exception.status_code, 409)
                finally:
                    release.set()
                    await asyncio.wait_for(first_task, timeout=3)
        async with self.session() as db:
            self.assertEqual((await db.scalar(select(Challenge))).description, "<p>First writer</p>")

    async def test_cancelled_import_rolls_back_and_releases_import_lock(self):
        await self.seed()
        entered = asyncio.Event()
        async def block_projection(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()
        async with self.session() as db:
            with patch("app.services.challenge_imports.project_rule_tag_mappings", new=block_projection):
                task = asyncio.create_task(self.import_data(db, [{"id": 1, "name": "Existing", "description": "Cancelled"}]))
                await asyncio.wait_for(entered.wait(), timeout=3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        async with self.session() as db:
            self.assertEqual((await db.scalar(select(Challenge))).description, "<p>Original</p>")
            self.assertEqual((await self.import_data(db, [{"id": 1, "name": "Existing", "description": "After cancellation"}]))["updated"], 1)


if __name__ == "__main__":
    unittest.main()
