"""Local Next rewrites and production Caddy must agree on CTF route ownership."""

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NEXT_CONFIG = PROJECT_ROOT / "frontend" / "next.config.ts"
CADDYFILE = PROJECT_ROOT / "infra" / "caddy" / "Caddyfile"


class CompetitionProxyRoutingContractTests(unittest.TestCase):
    def test_next_routes_protected_detail_and_file_to_learn_before_auth_catch_all(self) -> None:
        source = NEXT_CONFIG.read_text(encoding="utf-8")
        detail = 'source: "/api/learn/competitions/:competitionId/challenges/:challengeId"'
        file_download = (
            'source: "/api/learn/competitions/:competitionId/challenges/'
            ':challengeId/files/:fileIndex"'
        )
        auth_catch_all = 'source: "/api/learn/competitions/:path*"'

        self.assertLess(source.index(file_download), source.index(auth_catch_all))
        self.assertLess(source.index(detail), source.index(auth_catch_all))
        self.assertIn(
            "destination: `http://${learnHost}:${learnPort}/competitions/"
            ":competitionId/challenges/:challengeId/files/:fileIndex`",
            source,
        )
        self.assertIn(
            "destination: `http://${learnHost}:${learnPort}/competitions/"
            ":competitionId/challenges/:challengeId`",
            source,
        )

    def test_caddy_keeps_the_same_learn_route_before_auth_competition_routes(self) -> None:
        source = CADDYFILE.read_text(encoding="utf-8")
        learn_matcher = "@learn_competition_challenge path_regexp"
        auth_catch_all = "handle /ctf/api/learn/competitions/*"
        learn_block_start = source.index("handle @learn_competition_challenge")
        learn_block_end = source.index("# === 대회 메타데이터 API", learn_block_start)
        learn_block = source[learn_block_start:learn_block_end]

        self.assertLess(source.index(learn_matcher), source.index(auth_catch_all))
        self.assertLess(learn_block_start, source.index(auth_catch_all))
        self.assertIn("{$LEARN_HOST:localhost}:{$LEARN_PORT:8100}", learn_block)


if __name__ == "__main__":
    unittest.main()
