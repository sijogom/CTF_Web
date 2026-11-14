from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from app.api.knowledge_tags import _mapping_items_for_replace
from app.schemas.knowledge import ContentTagMappingUpdate


class KnowledgeTagIdOnlyReplaceTests(unittest.TestCase):
    def test_id_only_replace_preserves_existing_quality_and_defaults_new_tag(self) -> None:
        created_by = uuid4()
        created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        approved_at = datetime(2026, 1, 3, tzinfo=timezone.utc)
        previous = SimpleNamespace(
            tag_id=11,
            direction="content_to_knowledge_tag",
            strength=0.375,
            confidence=0.625,
            evidence_summary="reviewed evidence",
            origin="model_candidate",
            created_by=created_by,
            created_at=created_at,
            approved_at=approved_at,
        )

        items = _mapping_items_for_replace(
            ContentTagMappingUpdate(tag_ids=[11, 12]),
            {11: previous},
        )

        self.assertEqual([item.tag_id for item in items], [11, 12])
        self.assertEqual(items[0].strength, 0.375)
        self.assertEqual(items[0].confidence, 0.625)
        self.assertEqual(items[0].evidence_summary, "reviewed evidence")
        self.assertEqual(items[1].strength, 1.0)
        self.assertEqual(items[1].confidence, 1.0)
        self.assertEqual(items[1].evidence_summary, "수동 지식 태그 매핑")

    def test_explicit_mappings_remain_authoritative(self) -> None:
        previous = SimpleNamespace(
            tag_id=11,
            direction="content_to_knowledge_tag",
            strength=0.2,
            confidence=0.3,
            evidence_summary="old evidence",
        )
        body = ContentTagMappingUpdate.model_validate(
            {
                "tag_ids": [11],
                "mappings": [
                    {
                        "tag_id": 11,
                        "strength": 0.8,
                        "confidence": 0.9,
                        "evidence_summary": "explicit evidence",
                    }
                ],
            }
        )

        item = _mapping_items_for_replace(body, {11: previous})[0]

        self.assertEqual(item.strength, 0.8)
        self.assertEqual(item.confidence, 0.9)
        self.assertEqual(item.evidence_summary, "explicit evidence")


if __name__ == "__main__":
    unittest.main()
