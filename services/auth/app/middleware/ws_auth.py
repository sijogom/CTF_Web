"""WebSocket 인증 유틸리티."""

from fastapi import WebSocket, WebSocketException, status
from app.jwt.token import decode_token


async def authenticate_websocket(websocket: WebSocket) -> dict:
    """
    WebSocket 연결 시 JWT 토큰 검증.

    사용법:
        @app.websocket("/ws/example")
        async def ws_endpoint(websocket: WebSocket):
            payload = await authenticate_websocket(websocket)
            await websocket.accept()
            ...
    """
    # 쿼리 파라미터에서 토큰 추출 (WebSocket은 헤더 사용 불가)
    token = websocket.query_params.get("token")
    if not token:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)

    payload = decode_token(token)
    if not payload:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)

    return payload
