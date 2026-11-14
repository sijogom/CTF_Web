"""Caddy must route the V2 knowledge map to Learn, not Auth."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class KnowledgeGraphCaddyContractTests(unittest.TestCase):
    def test_v2_map_uses_learn_handler_after_competition_detail_exception(self) -> None:
        caddy = (PROJECT_ROOT / "infra" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        competition_route = caddy.index("handle @learn_competition_challenge")
        learn_catch_all = caddy.index("handle /ctf/api/learn/*")
        self.assertLess(competition_route, learn_catch_all)

        learn_block = caddy[learn_catch_all:]
        self.assertIn("uri strip_prefix /ctf/api/learn", learn_block)
        self.assertIn("reverse_proxy {$LEARN_HOST:localhost}:{$LEARN_PORT:8100}", learn_block)

    def test_competition_detail_exception_is_narrow_and_does_not_capture_v2_map(self) -> None:
        caddy = (PROJECT_ROOT / "infra" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
        pattern_start = caddy.index("^/ctf/api/learn/competitions/")
        pattern_end = caddy.index("$", pattern_start)
        pattern = caddy[pattern_start:pattern_end]
        self.assertIn("/challenges/", pattern)
        self.assertIn("[^/]+/challenges/[0-9]+", pattern)
        self.assertNotIn("knowledge-map", pattern)


if __name__ == "__main__":
    unittest.main()
