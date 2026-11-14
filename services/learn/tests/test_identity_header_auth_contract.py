"""Learn 인증 식별자 헤더 차단 계약을 런타임 의존성 없이 검증한다.

이 테스트는 실제 JWT 서명·Caddy 실행·Auth HTTP 호출을 수행하지 않는다. 대신 의존성
모듈을 대체한 순수 실행으로, 원시 X-User-* 헤더가 인증 경로에 도달하지 않고 서명된
JWT와 현재 Auth 상태 확인이 함께 요구되는지 확인한다.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4


SERVICE_ROOT = Path(__file__).resolve().parents[1]
DEPS = SERVICE_ROOT / "app" / "api" / "deps.py"
CADDYFILE = SERVICE_ROOT.parents[1] / "infra" / "caddy" / "Caddyfile"


class _HttpError(Exception):
    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _FakeJwtError(Exception):
    pass


class _FakeJwt:
    PyJWTError = _FakeJwtError

    def __init__(self, payload: dict | Exception) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, list[str]]] = []

    def decode(self, token: str, public_key: str, *, algorithms: list[str]) -> dict:
        self.calls.append((token, public_key, algorithms))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _FakeRequest:
    def __init__(self, *, cookies: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.state = SimpleNamespace()


def _load_dependencies(*, jwt_payload: dict | Exception, authorization_result: tuple[bool, str]) -> tuple[dict, _FakeJwt, list[str]]:
    """deps.py의 인증 함수만 fake JWT/Auth 경계로 분리해 실행한다."""
    source = DEPS.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted_names = {
        "_TokenAbsent",
        "_NO_TOKEN",
        "_user_id_from_payload",
        "_current_authorization",
        "_require_current_authorization",
        "_decode_bearer_token",
        "get_optional_user_id",
        "get_current_user_id",
        "get_current_user_name",
        "get_optional_user_role",
        "get_current_user_role",
        "require_role",
    }
    body = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign))
        and (
            (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in wanted_names for target in node.targets))
            or getattr(node, "name", None) in wanted_names
        )
    ]
    fake_jwt = _FakeJwt(jwt_payload)
    authorization_calls: list[str] = []

    async def fake_get_current_authorization(user_id: str) -> tuple[bool, str]:
        authorization_calls.append(user_id)
        return authorization_result

    namespace = {
        "UUID": UUID,
        "Request": object,
        "HTTPException": _HttpError,
        "status": SimpleNamespace(HTTP_401_UNAUTHORIZED=401, HTTP_403_FORBIDDEN=403),
        "jwt": fake_jwt,
        "settings": SimpleNamespace(jwt_public_key="test-public-key", jwt_algorithm="RS256"),
        "get_current_authorization": fake_get_current_authorization,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(DEPS), "exec"), namespace)
    return namespace, fake_jwt, authorization_calls


class LearnIdentityHeaderAuthenticationTests(unittest.TestCase):
    def test_raw_identity_headers_never_authenticate_and_never_reach_auth_lookup(self) -> None:
        user_id = uuid4()
        deps, _, authorization_calls = _load_dependencies(
            jwt_payload={"type": "access", "sub": str(user_id)},
            authorization_result=(True, "admin"),
        )
        request = _FakeRequest(
            headers={
                "x-user-id": str(user_id),
                "x-user-role": "admin",
                "x-user-name": "spoofed-admin",
            }
        )

        self.assertIsNone(asyncio.run(deps["get_optional_user_id"](request)))
        with self.assertRaises(_HttpError) as current_user_error:
            asyncio.run(deps["get_current_user_id"](request))
        self.assertEqual(current_user_error.exception.status_code, 401)

        checker = deps["require_role"]("admin")
        with self.assertRaises(_HttpError) as role_error:
            asyncio.run(checker(request))
        self.assertEqual(role_error.exception.status_code, 401)
        self.assertEqual(authorization_calls, [])

    def test_valid_cookie_jwt_rechecks_current_auth_state_before_returning_identity(self) -> None:
        user_id = uuid4()
        deps, fake_jwt, authorization_calls = _load_dependencies(
            jwt_payload={"type": "access", "sub": str(user_id), "username": "정상사용자"},
            authorization_result=(True, "admin"),
        )
        request = _FakeRequest(cookies={"access_token": "signed-cookie-token"})

        self.assertEqual(asyncio.run(deps["get_current_user_id"](request)), user_id)
        self.assertEqual(asyncio.run(deps["get_current_user_role"](request)), "admin")
        self.assertEqual(asyncio.run(deps["get_current_user_name"](request)), "정상사용자")
        self.assertEqual(authorization_calls, [str(user_id)])
        self.assertEqual(
            fake_jwt.calls,
            [
                ("signed-cookie-token", "test-public-key", ["RS256"]),
                ("signed-cookie-token", "test-public-key", ["RS256"]),
                ("signed-cookie-token", "test-public-key", ["RS256"]),
            ],
        )

    def test_valid_bearer_jwt_is_supported_without_identity_headers(self) -> None:
        user_id = uuid4()
        deps, fake_jwt, authorization_calls = _load_dependencies(
            jwt_payload={"type": "access", "sub": str(user_id)},
            authorization_result=(True, "user"),
        )
        request = _FakeRequest(headers={"authorization": "Bearer signed-api-token"})

        self.assertEqual(asyncio.run(deps["get_current_user_id"](request)), user_id)
        self.assertEqual(authorization_calls, [str(user_id)])
        self.assertEqual(fake_jwt.calls, [("signed-api-token", "test-public-key", ["RS256"])])

    def test_invalid_bearer_cannot_fall_back_to_raw_identity_headers(self) -> None:
        user_id = uuid4()
        deps, _, authorization_calls = _load_dependencies(
            jwt_payload=_FakeJwtError("invalid signature"),
            authorization_result=(True, "admin"),
        )
        request = _FakeRequest(
            headers={
                "authorization": "Bearer invalid-token",
                "x-user-id": str(user_id),
                "x-user-role": "admin",
            }
        )

        with self.assertRaises(_HttpError) as error:
            asyncio.run(deps["get_current_user_id"](request))
        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(authorization_calls, [])

    def test_dependency_signatures_do_not_expose_raw_identity_header_parameters(self) -> None:
        tree = ast.parse(DEPS.read_text(encoding="utf-8"))
        dependency_names = {
            "get_optional_user_id",
            "get_current_user_id",
            "get_current_user_name",
            "get_optional_user_role",
            "get_current_user_role",
        }
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in dependency_names:
                parameters = {argument.arg for argument in node.args.args}
                self.assertNotIn("x_user_id", parameters)
                self.assertNotIn("x_user_role", parameters)
                self.assertNotIn("x_user_name", parameters)

    def test_caddy_keeps_cookie_and_authorization_for_learn_proxy_static_contract(self) -> None:
        source = CADDYFILE.read_text(encoding="utf-8")
        learn_block_start = source.index("handle /ctf/api/learn/*")
        learn_block = source[learn_block_start:]

        self.assertIn("forward_auth", learn_block)
        self.assertIn("reverse_proxy {$LEARN_HOST:localhost}:{$LEARN_PORT:8100}", learn_block)
        self.assertIn("request_header -X-User-ID", source)
        self.assertNotIn("request_header -Authorization", source)
        self.assertNotIn("request_header -Cookie", source)


if __name__ == "__main__":
    unittest.main()
