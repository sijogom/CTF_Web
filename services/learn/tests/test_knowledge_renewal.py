"""SQL 공개 projection과 cursor/연결성 계약의 실제 in-memory DB 회귀."""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.api.knowledge_graph import _validated_graph_cursor
from app.schemas.knowledge import ContentTagMappingUpdate, RecommendationResponse
from app.services.knowledge_graph_pages import decode_graph_cursor, encode_graph_cursor, graph_page
from app.utils.content_tag_mappings import mapping_revision
from app.utils.knowledge_mapping_v2 import public_evidence_summary


class KnowledgeRenewalContractTests(unittest.TestCase):
    def test_mapping_revision_changes_quality_but_not_row_order(self):
        first = SimpleNamespace(tag_id=1, strength=0.2)
        second = SimpleNamespace(tag_id=2, strength=0.3)
        original = mapping_revision([first, second])
        self.assertEqual(original, mapping_revision([second, first]))
        first.strength = 0.4
        self.assertNotEqual(original, mapping_revision([first, second]))
        ContentTagMappingUpdate(mappings=[], expected_revision=original)

    def test_cursor_binds_all_context_and_rejects_new_revision(self):
        context = self.context()
        cursor = decode_graph_cursor(encode_graph_cursor(context, "mapping|challenge:1|knowledge_tag:1"))
        self.assertEqual(_validated_graph_cursor(cursor, context), cursor["after"])
        with self.assertRaises(HTTPException) as caught:
            _validated_graph_cursor(cursor, {**context, "revision": "epoch:2"})
        self.assertEqual(caught.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            decode_graph_cursor("not-a-cursor")
        import base64
        nested = base64.urlsafe_b64encode(("[" * 1500 + "0" + "]" * 1500).encode()).decode()
        with self.assertRaises(HTTPException) as caught:
            decode_graph_cursor(nested)
        self.assertEqual(caught.exception.status_code, 422)

    def test_sensitive_public_evidence_is_redacted(self):
        self.assertEqual(public_evidence_summary("flag{private-value}"), "관리자가 승인한 관계 근거")
        self.assertEqual(public_evidence_summary("공개 근거"), "공개 근거")

    def test_recommendation_empty_states_are_distinct(self):
        now = datetime.now(timezone.utc)
        for reason in ("no_history", "no_candidates"):
            response = RecommendationResponse(enabled=True, consent_required=False, policy_version="v1",
                                               generated_at=now, empty_reason=reason)
            self.assertEqual(response.empty_reason, reason)

    @staticmethod
    def context(**values):
        return {"mode": "canvas", "revision": "epoch:1", "viewer": None,
                "focus_type": None, "focus_id": None, "depth": 1,
                "types": ["challenge", "knowledge_tag"], "relation_types": ["tag_affinity"], **values}


class _SqliteSessionAdapter:
    """실제 SQLite 쿼리를 await 인터페이스로 연결한다. 비동기 드라이버가 필요 없다."""
    def __init__(self, engine):
        self.session = Session(engine)

    async def execute(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {})

    async def commit(self):
        self.session.commit()

    async def close(self):
        self.session.close()


class KnowledgeGraphSqlPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.db = _SqliteSessionAdapter(self.engine)
        definitions = [
            "challenges(id INTEGER PRIMARY KEY,name TEXT,is_published BOOLEAN,challenge_type TEXT)",
            "courses(id TEXT PRIMARY KEY,title TEXT,is_deleted BOOLEAN,is_published BOOLEAN,status TEXT)",
            "lessons(id TEXT PRIMARY KEY,course_id TEXT,title TEXT)",
            "knowledge_tags(id INTEGER PRIMARY KEY,name TEXT,parent_id INTEGER)",
            "hints(id INTEGER PRIMARY KEY,challenge_id INTEGER,order_index INTEGER)",
            "platform_events(id TEXT PRIMARY KEY,title TEXT,status TEXT,visible_from_at DATETIME,visible_until_at DATETIME)",
            "knowledge_content_nodes(id TEXT PRIMARY KEY,entity_type TEXT,entity_id TEXT)",
            "knowledge_relations(id TEXT PRIMARY KEY,source_node_id TEXT,target_node_id TEXT,relation_type TEXT,is_directed BOOLEAN,strength NUMERIC,confidence NUMERIC,evidence_summary TEXT,origin TEXT,created_at DATETIME,approved_at DATETIME,status TEXT)",
            "challenge_prerequisites(challenge_id INTEGER,prerequisite_id INTEGER)",
            "course_prerequisites(course_id TEXT,prerequisite_id TEXT)",
            "challenge_learning_prerequisites(challenge_id INTEGER,prerequisite_type TEXT,prerequisite_target_id TEXT)",
        ]
        for kind, identity in (("challenge", "INTEGER"), ("course", "TEXT"), ("lesson", "TEXT")):
            definitions.append(f"{kind}_knowledge_tags({kind}_id {identity},tag_id INTEGER,strength NUMERIC,confidence NUMERIC,evidence_summary TEXT,origin TEXT,created_at DATETIME,approved_at DATETIME)")
        for definition in definitions:
            await self.db.execute(text("CREATE TABLE " + definition))
        await self.db.execute(text("INSERT INTO knowledge_tags VALUES (1,'Web',NULL)"))
        await self.db.execute(text("INSERT INTO challenges VALUES (:id,:name,1,'wargame')"),
                              [{"id": identifier, "name": f"Challenge {identifier}"} for identifier in range(1, 131)])
        await self.db.execute(text("INSERT INTO challenge_knowledge_tags(challenge_id,tag_id,strength,confidence,evidence_summary,origin) VALUES (:id,1,0.9,0.8,'공개 근거','manual')"),
                              [{"id": identifier} for identifier in range(1, 131)])
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        self.engine.dispose()

    async def test_star_graph_pages_keep_tag_and_all_edges_without_duplicates(self):
        context = KnowledgeRenewalContractTests.context()
        seen = set()
        after = ""
        pages = 0
        while True:
            nodes, edges, cursor = await graph_page(self.db, context=context, after=after)
            keys = {node.id for node in nodes}
            self.assertLessEqual(len(nodes), 120)
            self.assertIn("knowledge_tag:1", keys)
            self.assertTrue(all(edge.source in keys and edge.target in keys for edge in edges))
            identifiers = {edge.id for edge in edges}
            self.assertFalse(seen & identifiers)
            seen |= identifiers
            pages += 1
            if cursor is None:
                break
            after = decode_graph_cursor(cursor)["after"]
            self.assertLess(pages, 5)
        self.assertEqual(len(seen), 130)
        self.assertEqual(pages, 2)

    async def test_focus_hops_are_real_sql_reachability(self):
        context = KnowledgeRenewalContractTests.context(mode="focus", focus_type="challenge", focus_id="1")
        nodes, edges, cursor = await graph_page(self.db, context=context)
        self.assertEqual({node.id for node in nodes}, {"challenge:1", "knowledge_tag:1"})
        self.assertEqual(len(edges), 1)
        self.assertIsNone(cursor)
        nodes, edges, cursor = await graph_page(self.db, context={**context, "depth": 2})
        self.assertEqual(len(nodes), 120)
        self.assertIsNotNone(cursor)
        self.assertTrue(all(node.distance in {0, 1, 2} for node in nodes))

    async def test_private_and_unapproved_mappings_are_never_public(self):
        await self.db.execute(text("UPDATE challenges SET is_published=0 WHERE id=1"))
        await self.db.execute(text("UPDATE challenge_knowledge_tags SET origin='model_candidate' WHERE challenge_id=2"))
        nodes, edges, cursor = await graph_page(self.db, context=KnowledgeRenewalContractTests.context())
        keys = {node.id for node in nodes}
        self.assertNotIn("challenge:1", keys)
        self.assertNotIn("challenge:2", keys)

    async def test_authoritative_requires_keeps_direction_origin_and_distances(self):
        await self.db.execute(text("INSERT INTO challenge_prerequisites(challenge_id,prerequisite_id) VALUES(2,1)"))
        context = KnowledgeRenewalContractTests.context(mode="focus", focus_type="challenge", focus_id="2",
            types=["challenge"], relation_types=["requires"])
        nodes, edges, cursor = await graph_page(self.db, context=context)
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0].source, edges[0].target), ("challenge:1", "challenge:2"))
        self.assertEqual(edges[0].origin, "system_prerequisite")
        self.assertEqual({node.id: node.distance for node in nodes}, {"challenge:1": 1, "challenge:2": 0})
        self.assertIsNone(cursor)


if __name__ == "__main__":
    unittest.main()
