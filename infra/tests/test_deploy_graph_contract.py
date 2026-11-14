"""The legacy Linux deploy path must not start stale graph images."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DeployGraphContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (PROJECT_ROOT / "infra" / "scripts" / "deploy.sh").read_text(
            encoding="utf-8"
        )

    def test_deploy_checks_v2_source_and_rebuilds_all_start_paths(self) -> None:
        self.assertIn("assert_knowledge_map_v2_source_contract()", self.script)
        self.assertIn("/api/learn/knowledge-map/v2/overview", self.script)
        self.assertIn("/api/learn/knowledge-map/v2/focus", self.script)
        self.assertIn('grep -Fq "/api/learn/knowledge-map/v2/search" "$graph_file"', self.script)
        self.assertIn('grep -Fq "/api/learn/knowledge-map/v2/canvas" "$graph_file"', self.script)
        self.assertIn("data-knowledge-map-version=\\\"v2\\\"", self.script)
        self.assertIn("data-map-layout=\\\"obsidian-canvas-v2\\\"", self.script)
        self.assertIn("data-map-ui-revision=\\\"free-canvas-click-focus-v3\\\"", self.script)
        self.assertIn("/api/learn/knowledge-graph", self.script)
        self.assertGreaterEqual(self.script.count("$COMPOSE_CMD up -d --build"), 4)

    def test_deploy_rejects_removed_knowledge_map_copy_and_global_classes_menu(self) -> None:
        self.assertIn('local learn_page="$PROJECT_DIR/frontend/src/app/[locale]/learn/page.tsx"', self.script)
        self.assertIn('local header_file="$PROJECT_DIR/frontend/src/components/layout/Header.tsx"', self.script)
        self.assertIn('for ui_file in "$graph_file" "$learn_page" "$header_file"; do', self.script)
        self.assertIn('"선택한 콘텐츠 중심의 지식 관계 지도"', self.script)
        self.assertIn('"다음 관계 더 보기"', self.script)
        self.assertIn('"배치 초기화"', self.script)
        self.assertIn('"내 강의"', self.script)

    def test_deploy_requires_external_jwt_files_without_key_generation(self) -> None:
        jwt_gate = self.script.split("ensure_jwt_keys() {", 1)[1].split(
            "assert_knowledge_map_v2_source_contract() {", 1
        )[0]
        self.assertIn("JWT_SECRET_KEY_SOURCE", jwt_gate)
        self.assertIn("JWT_PUBLIC_KEY_SOURCE", jwt_gate)
        self.assertIn("[ ! -f \"$source_path\" ]", jwt_gate)
        self.assertNotIn("openssl gen", jwt_gate)

    def test_update_pulls_before_v2_gate_to_recover_legacy_checkouts(self) -> None:
        update = self.script.split("    update)\n", 1)[1].split(
            "    logs)\n", 1
        )[0]
        self.assertLess(
            update.index('cd "$PROJECT_DIR" && git pull'),
            update.index("assert_knowledge_map_v2_source_contract"),
        )
        self.assertIn("$COMPOSE_CMD up -d --build", update)


if __name__ == "__main__":
    unittest.main()
