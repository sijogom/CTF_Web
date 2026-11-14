#!/bin/bash
# CTF 플랫폼 개발 환경 시작 스크립트 (WSL2 전용)
# 사용법: wsl bash scripts/dev-start.sh [infra|auth|learn|community|frontend|stop]
# 또는 WSL 터미널에서: bash scripts/dev-start.sh [...]

set -euo pipefail

# ⚠ 이 스크립트는 수동 터미널 실행용 레거시 스크립트입니다.
# 권장: bash infra/scripts/server.sh start (동적 포트 배정 + 전체 자동화)

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# 포트 — 환경변수 또는 기본값
AUTH_PORT=${AUTH_PORT:-8020}
LEARN_PORT=${LEARN_PORT:-8100}
COMMUNITY_PORT=${COMMUNITY_PORT:-8300}
FRONTEND_PORT=${FRONTEND_PORT:-3100}

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

case "${1:-help}" in
  infra)
    echo -e "${CYAN}[인프라]${NC} PostgreSQL, Redis, MinIO, Mailpit 시작..."
    docker compose -f infra/docker-compose.dev.yml up -d
    echo ""
    echo -e "${GREEN}=== 인프라 서비스 ===${NC}"
    echo -e "  PostgreSQL : localhost:5432"
    echo -e "  Redis      : localhost:6379"
    echo -e "  MinIO      : localhost:9000  (콘솔: http://localhost:9001)"
    echo -e "  Mailpit    : localhost:1025  (웹UI: http://localhost:8025)"
    echo ""
    echo -e "${YELLOW}첫 실행 시 DB 초기화에 약 10초 소요됩니다.${NC}"
    echo -e "상태 확인: docker compose -f infra/docker-compose.dev.yml ps"
    ;;

  auth)
    echo -e "${CYAN}[auth-service]${NC} http://localhost:$AUTH_PORT 시작..."
    cd services/auth
    source .venv-wsl/bin/activate
    python -m uvicorn app.main:app --reload --host 0.0.0.0 --port "$AUTH_PORT"
    ;;

  learn)
    echo -e "${CYAN}[learn-service]${NC} http://localhost:$LEARN_PORT 시작..."
    cd services/learn
    source .venv-wsl/bin/activate
    python -m uvicorn app.main:app --reload --host 0.0.0.0 --port "$LEARN_PORT"
    ;;

  community)
    echo -e "${CYAN}[community-service]${NC} http://localhost:$COMMUNITY_PORT 시작..."
    cd services/community
    source .venv-wsl/bin/activate
    python -m uvicorn app.main:app --reload --host 0.0.0.0 --port "$COMMUNITY_PORT"
    ;;

  frontend)
    echo -e "${CYAN}[frontend]${NC} http://localhost:$FRONTEND_PORT 시작..."
    cd frontend
    npm run dev -- -p "$FRONTEND_PORT"
    ;;

  stop)
    echo -e "${RED}[중지]${NC} 인프라 서비스 중지..."
    docker compose -f infra/docker-compose.dev.yml down
    echo -e "${GREEN}완료${NC}"
    ;;

  *)
    echo -e "${CYAN}CTF 플랫폼 개발 환경 스크립트 (WSL2 전용)${NC}"
    echo ""
    echo "사용법 (WSL 터미널에서 실행):"
    echo "  bash scripts/dev-start.sh infra       # 1단계: 인프라 시작 (DB/Redis/MinIO)"
    echo "  bash scripts/dev-start.sh auth        # 2단계: auth-service (새 터미널)"
    echo "  bash scripts/dev-start.sh learn       # 3단계: learn-service (새 터미널)"
    echo "  bash scripts/dev-start.sh community   # 4단계: community-service (새 터미널)"
    echo "  bash scripts/dev-start.sh frontend    # 5단계: Next.js 프론트엔드 (새 터미널)"
    echo "  bash scripts/dev-start.sh stop        # 인프라 중지"
    echo ""
    echo "각 서비스는 별도 WSL 터미널에서 실행하세요. (Hot Reload 지원)"
    echo ""
    echo "포트 매핑 (환경변수로 변경 가능):"
    echo "  auth-service      : $AUTH_PORT (AUTH_PORT)"
    echo "  learn-service     : $LEARN_PORT (LEARN_PORT)"
    echo "  community-service : $COMMUNITY_PORT (COMMUNITY_PORT)"
    echo "  frontend          : $FRONTEND_PORT (FRONTEND_PORT)"
    echo "  PostgreSQL        : 5432"
    echo "  Redis             : 6379"
    echo "  MinIO             : 9000/9001"
    echo "  Mailpit           : 1025/8025"
    ;;
esac
