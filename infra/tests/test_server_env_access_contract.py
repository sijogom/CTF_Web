"""Static checks for the production environment-access boundary.

These checks deliberately inspect source only. They do not start Docker, WSL,
or a server and therefore cannot prove runtime proxy reachability.
"""

from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ServerEnvironmentAccessContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (PROJECT_ROOT / "infra" / "scripts" / "server.sh").read_text(
            encoding="utf-8"
        )
        self.production_compose = (
            PROJECT_ROOT / "infra" / "docker-compose.prod.yml"
        ).read_text(encoding="utf-8")

    def test_host_mode_production_frontend_uses_proxy_not_direct_ports(self) -> None:
        match = re.search(
            r'if \[ "\$IS_PROD" = true \]; then(?P<production>.*?)\n\s*else\n'
            r'\s*# 개발: proxy 모드',
            self.script,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "start_frontend production branch is missing")
        production = match.group("production")

        self.assertIn("NEXT_PUBLIC_ENV_ACCESS_MODE=proxy", production)
        self.assertIn("NEXT_PUBLIC_ENV_PROXY_BASE=", production)
        self.assertNotIn("NEXT_PUBLIC_ENV_ACCESS_MODE=direct", production)
        # Windows `localhost` may resolve to IPv6 while Next's middleware
        # internally proxies the basePath request to localhost. Bind on all
        # interfaces for this host-mode fallback; the public access contract
        # remains proxy-only and is enforced above.
        self.assertIn("--hostname 0.0.0.0", production)

    def test_compose_production_contract_remains_relative_proxy(self) -> None:
        self.assertIn("NEXT_PUBLIC_ENV_PROXY_BASE: \"\"", self.production_compose)
        self.assertIn("NEXT_PUBLIC_ENV_ACCESS_MODE: proxy", self.production_compose)

    def test_source_production_start_rebuilds_and_requires_v2_map(self) -> None:
        self.assertIn("assert_knowledge_map_v2_source_contract", self.script)
        start_docker = self.script.split("start_docker() {", 1)[1].split(
            "stop_docker() {", 1
        )[0]
        self.assertIn("assert_knowledge_map_v2_source_contract", start_docker)
        self.assertIn("$compose_cmd up -d --build", start_docker)
        self.assertIn("/api/learn/knowledge-map/v2/overview", self.script)
        self.assertIn("/api/learn/knowledge-map/v2/focus", self.script)
        self.assertIn('grep -Fq "/api/learn/knowledge-map/v2/search" "$graph_file"', self.script)
        self.assertIn('grep -Fq "/api/learn/knowledge-map/v2/canvas" "$graph_file"', self.script)

    def test_production_start_rejects_removed_knowledge_map_copy_and_global_classes_menu(self) -> None:
        self.assertIn('local learn_page="$FRONTEND_DIR/src/app/[locale]/learn/page.tsx"', self.script)
        self.assertIn('local header_file="$FRONTEND_DIR/src/components/layout/Header.tsx"', self.script)
        self.assertIn('for ui_file in "$graph_file" "$learn_page" "$header_file"; do', self.script)
        self.assertIn('"선택한 콘텐츠 중심의 지식 관계 지도"', self.script)
        self.assertIn('"다음 관계 더 보기"', self.script)
        self.assertIn('"배치 초기화"', self.script)
        self.assertIn('"내 강의"', self.script)
        self.assertIn("data-knowledge-map-version=\\\"v2\\\"", self.script)
        self.assertIn("data-map-layout=\\\"obsidian-canvas-v2\\\"", self.script)
        self.assertIn("data-map-ui-revision=\\\"free-canvas-click-focus-v3\\\"", self.script)
        self.assertIn("/api/learn/knowledge-graph", self.script)

    def test_production_jwt_gate_requires_external_files_without_generating_keys(self) -> None:
        jwt_gate = self.script.split("ensure_jwt_keys() {", 1)[1].split(
            "# ──── 상태 확인", 1
        )[0]
        self.assertIn("JWT_SECRET_KEY_SOURCE", jwt_gate)
        self.assertIn("JWT_PUBLIC_KEY_SOURCE", jwt_gate)
        self.assertIn("[ ! -f \"$source_path\" ]", jwt_gate)
        self.assertNotIn("openssl gen", jwt_gate)
        self.assertNotIn("openssl genrsa", jwt_gate)


if __name__ == "__main__":
    unittest.main()
