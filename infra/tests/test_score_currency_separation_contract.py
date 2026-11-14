"""CTF score, user points, and marketplace currency remain separate contracts."""

import ast
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTH_INTERNAL = PROJECT_ROOT.parent / "services" / "auth" / "app" / "api" / "internal.py"
AUTH_COMPETITION = PROJECT_ROOT.parent / "services" / "auth" / "app" / "models" / "competition.py"
AUTH_USER = PROJECT_ROOT.parent / "services" / "auth" / "app" / "models" / "user.py"
MARKETPLACE = PROJECT_ROOT.parent / "services" / "community" / "app" / "api" / "marketplace.py"
MARKETPLACE_GATEWAY = PROJECT_ROOT.parent / "services" / "community" / "app" / "utils" / "marketplace_settlement.py"
POINT_TRANSFER = PROJECT_ROOT.parent / "services" / "auth" / "app" / "utils" / "point_transfers.py"


def _function(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


class ScoreCurrencySeparationContractTests(unittest.TestCase):
    def test_models_have_distinct_storage_fields(self) -> None:
        competition = AUTH_COMPETITION.read_text(encoding="utf-8")
        user = AUTH_USER.read_text(encoding="utf-8")
        self.assertIn("class CompetitionParticipant", competition)
        self.assertIn("score = Column(Integer", competition)
        self.assertIn("points = Column(Integer", user)
        self.assertNotEqual(
            competition.index("score = Column(Integer"),
            user.index("points = Column(Integer"),
        )

    def test_competition_score_route_updates_only_participant_score(self) -> None:
        source = AUTH_INTERNAL.read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = _function(tree, "update_competition_score")
        function_source = ast.get_source_segment(source, function) or ""
        self.assertIn("update(CompetitionParticipant)", function_source)
        self.assertIn("CompetitionParticipant.score + body.score_delta", function_source)
        self.assertNotIn("User.points", function_source)
        self.assertNotIn("User.xp", function_source)

    def test_marketplace_uses_points_internal_api_not_ctf_score(self) -> None:
        source = MARKETPLACE.read_text(encoding="utf-8")
        gateway = MARKETPLACE_GATEWAY.read_text(encoding="utf-8")
        transfer = POINT_TRANSFER.read_text(encoding="utf-8")
        self.assertIn("reconcile_operation", source)
        self.assertIn("TransferGateway", source)
        self.assertIn("/internal/points/transfers/{operation.id}", gateway)
        self.assertIn("/internal/points/transfers", gateway)
        self.assertIn("settle_marketplace_transfer", AUTH_INTERNAL.read_text(encoding="utf-8"))
        self.assertIn("buyer.points -=", transfer)
        self.assertIn("seller.points +=", transfer)
        for module in (source, gateway, transfer):
            self.assertNotIn("/internal/competition/score", module)
            self.assertNotIn("CompetitionParticipant", module)
        self.assertNotIn(".xp", transfer)


if __name__ == "__main__":
    unittest.main()
