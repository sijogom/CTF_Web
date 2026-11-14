"""설정·DB 없이 모더레이션 순수 helper의 핵심 보안 계약을 검증한다."""

import ast
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Literal


_HELPER_NAMES = {
    "confirmation_token_digest",
    "confirmation_is_expired",
    "direct_delete_policy",
    "assert_direct_owner_delete_allowed",
    "_constant_time_name_match",
    "_display_name",
    "_target_is_hidden",
    "_target_is_deleted",
    "target_state_fingerprint",
    "_set_target_hidden",
}


class _HTTPException(Exception):
    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _load_pure_helpers() -> dict[str, object]:
    """서비스 설정·DB 초기화 없이 moderation.py의 순수 함수만 로드한다."""
    source_path = Path(__file__).resolve().parents[1] / "app" / "api" / "moderation.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    helper_nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _HELPER_NAMES
    ]

    class Post:
        pass

    class Comment:
        pass

    class MarketplaceItem:
        pass

    class MarketplaceComment:
        pass

    namespace: dict[str, object] = {
        "hashlib": hashlib,
        "hmac": hmac,
        "json": json,
        "datetime": datetime,
        "timezone": timezone,
        "Literal": Literal,
        "HTTPException": _HTTPException,
        "status": SimpleNamespace(
            HTTP_403_FORBIDDEN=403,
            HTTP_409_CONFLICT=409,
        ),
        "TargetType": str,
        "ResolvedModerationTarget": object,
        "Post": Post,
        "Comment": Comment,
        "MarketplaceItem": MarketplaceItem,
        "MarketplaceComment": MarketplaceComment,
    }
    module = ast.Module(body=helper_nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace


class ModerationPureHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.helpers = _load_pure_helpers()

    def test_token_digest_is_sha256_and_deterministic(self) -> None:
        digest = self.helpers["confirmation_token_digest"]

        self.assertEqual(
            digest("confirmation-token"),
            hashlib.sha256(b"confirmation-token").hexdigest(),
        )
        self.assertNotEqual(digest("confirmation-token"), digest("other-token"))

    def test_expiry_rejects_exact_boundary_and_accepts_future_token(self) -> None:
        is_expired = self.helpers["confirmation_is_expired"]
        now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

        self.assertTrue(is_expired(now, now))
        self.assertTrue(is_expired(now - timedelta(seconds=1), now))
        self.assertFalse(is_expired(now + timedelta(seconds=1), now))
        self.assertTrue(is_expired(now.replace(tzinfo=None), now))

    def test_direct_delete_policy_prevents_privileged_bypass(self) -> None:
        policy = self.helpers["direct_delete_policy"]
        assert_allowed = self.helpers["assert_direct_owner_delete_allowed"]

        self.assertEqual(policy("user", True), "allow_owner")
        self.assertEqual(policy(None, True), "allow_owner")
        self.assertEqual(policy("user", False), "forbidden")
        self.assertEqual(policy("admin", True), "requires_moderation")
        self.assertEqual(policy("moderator", False), "requires_moderation")

        with self.assertRaises(_HTTPException) as admin_error:
            assert_allowed("admin", True)
        self.assertEqual(admin_error.exception.status_code, 409)

        with self.assertRaises(_HTTPException) as owner_error:
            assert_allowed("user", False)
        self.assertEqual(owner_error.exception.status_code, 403)

    def test_target_fingerprint_changes_when_content_or_visibility_changes(self) -> None:
        fingerprint = self.helpers["target_state_fingerprint"]
        target = SimpleNamespace(
            id="post-1",
            title="Target title",
            content="Initial content",
            is_hidden=False,
        )

        initial = fingerprint("post", target)
        target.content = "Changed content"
        self.assertNotEqual(initial, fingerprint("post", target))

        target.content = "Initial content"
        target.is_hidden = True
        self.assertNotEqual(initial, fingerprint("post", target))

    def test_action_state_uses_existing_visibility_fields(self) -> None:
        set_hidden = self.helpers["_set_target_hidden"]

        post = SimpleNamespace(is_hidden=False)
        post_target = SimpleNamespace(target_type="post", entity=post, is_hidden=False)
        set_hidden(post_target, True)
        self.assertTrue(post.is_hidden)
        self.assertTrue(post_target.is_hidden)
        set_hidden(post_target, False)
        self.assertFalse(post.is_hidden)

        item = SimpleNamespace(status="active")
        item_target = SimpleNamespace(
            target_type="marketplace_item",
            entity=item,
            is_hidden=False,
        )
        set_hidden(item_target, True)
        self.assertEqual(item.status, "hidden")
        set_hidden(item_target, False)
        self.assertEqual(item.status, "active")


def _load_visibility_helpers() -> dict[str, object]:
    """FastAPI 앱 초기화 없이 가시성 정책의 순수 helper만 로드한다."""
    source_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "utils"
        / "content_visibility.py"
    )
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    helper_names = {
        "is_moderation_role",
        "can_access_content",
        "assert_content_visible",
    }
    assigned_names = {
        "MODERATION_ROLES",
        "PUBLIC_ACCESS",
        "MODERATION_READ_ACCESS",
        "VisibilityScope",
        "_NOT_FOUND_DETAIL",
    }

    helper_nodes: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in helper_names:
            helper_nodes.append(node)
        elif isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id in assigned_names
                for target in node.targets
            ):
                helper_nodes.append(node)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id in assigned_names:
                helper_nodes.append(node)

    namespace: dict[str, object] = {
        "Literal": Literal,
        "HTTPException": _HTTPException,
        "status": SimpleNamespace(HTTP_404_NOT_FOUND=404),
    }
    module = ast.Module(body=helper_nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace


def _route_sources(path: Path) -> dict[str, str]:
    """route 함수별 원문을 반환해 가시성 가드 누락 회귀를 정적으로 검증한다."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    return {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
    }


class ContentVisibilityHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.helpers = _load_visibility_helpers()

    def test_hidden_content_only_allows_moderation_read_for_moderation_roles(self) -> None:
        can_access = self.helpers["can_access_content"]

        self.assertTrue(
            can_access(is_hidden=False, role=None, scope="public")
        )
        self.assertFalse(
            can_access(is_hidden=True, role="user", scope="public")
        )
        self.assertFalse(
            can_access(is_hidden=True, role="admin", scope="public")
        )
        self.assertFalse(
            can_access(is_hidden=True, role="user", scope="moderation_read")
        )
        self.assertTrue(
            can_access(is_hidden=True, role="admin", scope="moderation_read")
        )
        self.assertTrue(
            can_access(is_hidden=True, role="moderator", scope="moderation_read")
        )

    def test_hidden_public_access_returns_non_enumerating_not_found(self) -> None:
        assert_visible = self.helpers["assert_content_visible"]

        with self.assertRaises(_HTTPException) as hidden_error:
            assert_visible(is_hidden=True, role="user")
        self.assertEqual(hidden_error.exception.status_code, 404)
        self.assertEqual(hidden_error.exception.detail, "요청한 콘텐츠를 찾을 수 없습니다.")

        with self.assertRaises(_HTTPException) as resource_error:
            assert_visible(
                is_hidden=True,
                role="user",
                not_found_detail="게시글을 찾을 수 없습니다.",
            )
        self.assertEqual(resource_error.exception.status_code, 404)
        self.assertEqual(resource_error.exception.detail, "게시글을 찾을 수 없습니다.")

        assert_visible(
            is_hidden=True,
            role="admin",
            scope="moderation_read",
        )


class ContentVisibilityRouteCoverageTests(unittest.TestCase):
    def test_post_detail_and_public_mutations_use_the_post_visibility_guard(self) -> None:
        routes = _route_sources(
            Path(__file__).resolve().parents[1] / "app" / "api" / "posts.py"
        )
        guarded_routes = {
            "get_post",
            "approve_post",
            "reject_post",
            "toggle_pin",
            "create_comment",
            "toggle_like",
            "toggle_dislike",
            "toggle_bookmark",
            "update_post",
            "delete_post",
            "update_comment",
            "delete_comment",
        }
        for route_name in guarded_routes:
            self.assertIn("_get_post_for_visibility", routes[route_name], route_name)

        self.assertIn("moderation_read=True", routes["get_post"])
        self.assertIn("Post.is_hidden.is_(False)", routes["list_pending_posts"])

    def test_marketplace_public_paths_and_history_filter_hidden_items(self) -> None:
        routes = _route_sources(
            Path(__file__).resolve().parents[1]
            / "app"
            / "api"
            / "marketplace.py"
        )
        guarded_routes = {
            "get_item",
            "purchase_item",
            "check_purchased",
            "download_item",
            "list_item_comments",
            "create_item_comment",
            "update_item",
            "delete_item",
            "update_item_comment",
            "delete_item_comment",
        }
        for route_name in guarded_routes:
            self.assertIn("_get_item_for_visibility", routes[route_name], route_name)

        self.assertIn("moderation_read=True", routes["get_item"])
        self.assertIn('item_alias.status != "hidden"', routes["my_purchases"])
        self.assertIn('MarketplaceItem.status != "hidden"', routes["my_items"])


if __name__ == "__main__":
    unittest.main()
