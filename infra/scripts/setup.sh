#!/bin/bash
# ═══════════════════════════════════════════════════════════
# CTF 학습 플랫폼 — 초기 설정 스크립트
# 새 호스트에서 처음 설정하거나 .env 변경 후 실행
#
# 사용법: bash infra/scripts/setup.sh
# (프로젝트 루트 또는 infra/ 어디서든 실행 가능)
# ═══════════════════════════════════════════════════════════
set -euo pipefail

# ──── 경로 자동 감지 ────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$(cd "$INFRA_DIR/.." && pwd)"

ENV_FILE="$INFRA_DIR/.env"

# ──── 색상 ────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $1"; }
log_info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

# ──── .env 검증 ────
if [ ! -f "$ENV_FILE" ]; then
    log_fail ".env 파일이 없습니다: $ENV_FILE"
    log_info ".env.example을 복사하세요: cp $INFRA_DIR/.env.example $ENV_FILE"
    exit 1
fi

log_ok "중앙 .env 파일 확인: $ENV_FILE"

# ──── .env 로드 (특수문자 보존) ────
# bash source는 따옴표/대괄호를 해석하므로, grep으로 원본 값 추출
get_env() {
    local key=$1
    grep "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2-
}

# 일반 변수는 source로 로드 (특수문자 없는 값들)
while IFS='=' read -r key value; do
    # 주석/빈줄 스킵
    [[ -z "$key" || "$key" =~ ^# ]] && continue
    # CORS_ORIGINS는 source하면 깨지므로 제외
    [[ "$key" == "CORS_ORIGINS" ]] && continue
    export "$key=$value"
done < "$ENV_FILE"

# CORS_ORIGINS는 원본 그대로 보존
CORS_ORIGINS_RAW=$(get_env "CORS_ORIGINS")

# ──── 서비스별 .env 생성 ────
log_info "서비스별 .env 파일 생성 중..."

# auth-service
cat > "$PROJECT_DIR/services/auth/.env" << EOF
# auth-service 로컬 개발용 환경변수 (infra/scripts/setup.sh 자동 생성)
DATABASE_URL=postgresql+asyncpg://auth_svc:${AUTH_DB_PASSWORD}@127.0.0.1:5432/platform_db
REDIS_URL=redis://:${REDIS_PASSWORD}@127.0.0.1:6379/0
ADMIN_USERNAME=${ADMIN_USERNAME}
ADMIN_EMAIL=${ADMIN_EMAIL}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
JWT_SECRET_KEY=${JWT_SECRET_KEY}
JWT_PUBLIC_KEY=
JWT_ALGORITHM=${JWT_ALGORITHM}
CTFD_URL=${CTFD_URL}
CORS_ORIGINS=${CORS_ORIGINS_RAW}
FRONTEND_URL=${FRONTEND_URL}
ENVIRONMENT=${ENVIRONMENT}
SMTP_HOST=${SMTP_HOST}
SMTP_PORT=${SMTP_PORT}
INTERNAL_API_KEY=${INTERNAL_API_KEY}
SITE_DOMAIN=${SITE_DOMAIN}
EOF
log_ok "services/auth/.env 생성"

# learn-service
cat > "$PROJECT_DIR/services/learn/.env" << EOF
# learn-service 로컬 개발용 환경변수 (infra/scripts/setup.sh 자동 생성)
DATABASE_URL=postgresql+asyncpg://learn_svc:${LEARN_DB_PASSWORD}@127.0.0.1:5432/learn_db
AUTH_SERVICE_URL=http://localhost:${AUTH_PORT:-8020}
CORS_ORIGINS=${CORS_ORIGINS_RAW}
ENVIRONMENT=${ENVIRONMENT}
JWT_PUBLIC_KEY=${JWT_SECRET_KEY}
JWT_ALGORITHM=${JWT_ALGORITHM}
REDIS_URL=redis://:${REDIS_PASSWORD}@127.0.0.1:6379/0
MINIO_ENDPOINT=${MINIO_ENDPOINT}
MINIO_ACCESS_KEY=${MINIO_ROOT_USER}
MINIO_SECRET_KEY=${MINIO_ROOT_PASSWORD}
MINIO_SECURE=${MINIO_SECURE}
INTERNAL_API_KEY=${INTERNAL_API_KEY}
SITE_DOMAIN=${SITE_DOMAIN}
DYNAMIC_INSTANCE_FLAGS_ENABLED=${DYNAMIC_INSTANCE_FLAGS_ENABLED:-false}
DYNAMIC_FLAG_HMAC_KEYRING=${DYNAMIC_FLAG_HMAC_KEYRING:-}
EOF
log_ok "services/learn/.env 생성"

# community-service
cat > "$PROJECT_DIR/services/community/.env" << EOF
# community-service 로컬 개발용 환경변수 (infra/scripts/setup.sh 자동 생성)
DATABASE_URL=postgresql+asyncpg://community_svc:${COMMUNITY_DB_PASSWORD}@127.0.0.1:5432/community_db
AUTH_SERVICE_URL=http://localhost:${AUTH_PORT:-8020}
CORS_ORIGINS=${CORS_ORIGINS_RAW}
ENVIRONMENT=${ENVIRONMENT}
JWT_PUBLIC_KEY=${JWT_SECRET_KEY}
JWT_ALGORITHM=${JWT_ALGORITHM}
MINIO_ENDPOINT=${MINIO_ENDPOINT}
MINIO_ACCESS_KEY=${MINIO_ROOT_USER}
MINIO_SECRET_KEY=${MINIO_ROOT_PASSWORD}
MINIO_SECURE=${MINIO_SECURE}
INTERNAL_API_KEY=${INTERNAL_API_KEY}
SITE_DOMAIN=${SITE_DOMAIN}
EOF
log_ok "services/community/.env 생성"

# ──── Docker Compose 검증 ────
log_info "Docker Compose 설정 검증 중..."
cd "$INFRA_DIR"
if docker compose -p ctf_web -f docker-compose.dev.yml config --quiet 2>/dev/null; then
    log_ok "docker-compose.dev.yml 유효"
else
    log_fail "docker-compose.dev.yml 검증 실패"
    exit 1
fi

# ──── Docker 컨테이너 시작 ────
log_info "Docker 인프라 컨테이너 시작 중..."
cd "$INFRA_DIR"
docker compose -p ctf_web -f docker-compose.dev.yml up -d

# ──── 헬스체크 ────
log_info "PostgreSQL 헬스체크 대기 중..."
for i in $(seq 1 30); do
    if docker exec ctf-postgres pg_isready -U "${POSTGRES_USER}" > /dev/null 2>&1; then
        log_ok "PostgreSQL 정상"
        break
    fi
    if [ "$i" -eq 30 ]; then
        log_fail "PostgreSQL 시작 실패"
        exit 1
    fi
    sleep 1
done

log_info "Redis 헬스체크..."
if docker exec ctf-redis redis-cli -a "${REDIS_PASSWORD}" ping 2>/dev/null | grep -q PONG; then
    log_ok "Redis 정상"
else
    log_fail "Redis 연결 실패"
fi

echo ""
echo "═══════════════════════════════════════════════════"
echo "  설정 완료! 서버 시작: bash infra/scripts/server.sh start"
echo "═══════════════════════════════════════════════════"
