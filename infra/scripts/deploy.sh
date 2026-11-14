#!/bin/bash
# ═══════════════════════════════════════════════════════════
# CTF 학습 플랫폼 — 프로덕션 배포 스크립트 (Linux 네이티브)
#
# 원클릭 설치: bash infra/scripts/deploy.sh install
# 서비스 관리: bash infra/scripts/deploy.sh [start|stop|status|update|logs]
#
# 대상 환경: Ubuntu 20.04+, Debian 11+
# Windows WSL2: deploy-wsl.sh 사용
# ═══════════════════════════════════════════════════════════

set -euo pipefail

# ──── 경로 자동 감지 ────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_DIR="$(dirname "$INFRA_DIR")"
COMPOSE_FILE="$INFRA_DIR/docker-compose.yml"
PROD_FILE="$INFRA_DIR/docker-compose.prod.yml"
ENV_FILE="$INFRA_DIR/.env"
ENV_EXAMPLE="$INFRA_DIR/.env.example"
AUTH_DIR="$PROJECT_DIR/services/auth"

# ──── 색상 ────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "\n${CYAN}${BOLD}── $* ──${NC}"; }

COMPOSE_CMD="docker compose -p ctf_web -f $COMPOSE_FILE -f $PROD_FILE"

# ──── 유틸리티 ────
gen_password() { head -c 48 /dev/urandom 2>/dev/null | base64 2>/dev/null | tr -dc 'A-Za-z0-9' | head -c 32; }

need_sudo() {
    if [ "$EUID" -ne 0 ]; then
        if command -v sudo &>/dev/null; then echo "sudo"; else echo ""; fi
    else
        echo ""
    fi
}

# ──── 1단계: 시스템 패키지 설치 ────
install_system_packages() {
    log_step "시스템 패키지 설치"

    local SUDO
    SUDO=$(need_sudo)

    # OS 감지
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        log_info "OS: $PRETTY_NAME"
    fi

    # 패키지 관리자 감지
    if command -v apt-get &>/dev/null; then
        install_apt "$SUDO"
    elif command -v dnf &>/dev/null; then
        install_dnf "$SUDO"
    elif command -v yum &>/dev/null; then
        install_yum "$SUDO"
    else
        log_warn "지원되지 않는 패키지 관리자입니다. 수동으로 설치하세요:"
        log_info "  필수: docker, docker-compose-plugin, openssl, curl, git"
        return 1
    fi
}

install_apt() {
    local SUDO=$1
    log_info "apt 패키지 설치 중..."

    $SUDO apt-get update -qq

    # 기본 도구
    $SUDO apt-get install -y -qq \
        ca-certificates curl gnupg lsb-release git openssl wget 2>/dev/null
    log_ok "기본 도구 설치 완료"

    # Docker 설치 (공식 저장소)
    if ! command -v docker &>/dev/null; then
        log_info "Docker 설치 중..."
        $SUDO install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
        $SUDO chmod a+r /etc/apt/keyrings/docker.gpg
        echo \
            "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
            $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
            $SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null
        $SUDO apt-get update -qq
        $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin 2>/dev/null
        log_ok "Docker 설치 완료"
    else
        log_ok "Docker 이미 설치됨"
    fi

    # Docker 서비스 시작
    $SUDO systemctl start docker 2>/dev/null || true
    $SUDO systemctl enable docker 2>/dev/null || true

    # 현재 사용자를 docker 그룹에 추가
    if ! groups | grep -q docker; then
        $SUDO usermod -aG docker "$USER" 2>/dev/null || true
        log_warn "docker 그룹에 추가됨 — 반영하려면 'newgrp docker' 또는 재로그인 필요"
    fi

    # ufw 설치 (방화벽)
    if ! command -v ufw &>/dev/null; then
        $SUDO apt-get install -y -qq ufw 2>/dev/null
    fi

    log_ok "시스템 패키지 설치 완료"
}

install_dnf() {
    local SUDO=$1
    log_info "dnf 패키지 설치 중..."
    $SUDO dnf install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin openssl curl git 2>/dev/null || {
        # Docker 공식 리포가 없을 수 있음
        $SUDO dnf install -y -q docker openssl curl git 2>/dev/null
    }
    $SUDO systemctl start docker 2>/dev/null || true
    $SUDO systemctl enable docker 2>/dev/null || true
    log_ok "시스템 패키지 설치 완료"
}

install_yum() {
    local SUDO=$1
    log_info "yum 패키지 설치 중..."
    $SUDO yum install -y -q docker openssl curl git 2>/dev/null
    $SUDO systemctl start docker 2>/dev/null || true
    $SUDO systemctl enable docker 2>/dev/null || true
    log_ok "시스템 패키지 설치 완료"
}

# ──── 2단계: 사전 요구사항 확인 ────
check_prerequisites() {
    log_step "사전 요구사항 확인"
    local missing=0

    for cmd in docker openssl curl git; do
        if command -v $cmd &>/dev/null; then
            log_ok "$cmd"
        else
            log_error "$cmd 미설치"
            missing=1
        fi
    done

    if docker compose version &>/dev/null; then
        log_ok "Docker Compose $(docker compose version --short 2>/dev/null)"
    else
        log_error "Docker Compose 미설치"
        missing=1
    fi

    # Docker 소켓 접근 확인
    if docker info &>/dev/null; then
        log_ok "Docker 데몬 접근 가능"
    else
        log_error "Docker 데몬에 접근할 수 없습니다 (권한 또는 서비스 확인)"
        missing=1
    fi

    if [ $missing -eq 1 ]; then
        log_error "필수 도구가 누락되었습니다."
        exit 1
    fi
}

# ──── 3단계: .env 자동 생성 ────
setup_env() {
    log_step ".env 설정"

    if [ -f "$ENV_FILE" ]; then
        log_ok ".env 파일 존재"
        if grep -q "CHANGE_ME\|YOUR_DOMAIN" "$ENV_FILE"; then
            log_warn "기본값(CHANGE_ME/YOUR_DOMAIN)이 남아있습니다"
            read -p "자동으로 안전한 비밀번호를 생성할까요? (Y/n) " -r
            if [[ ! $REPLY =~ ^[Nn]$ ]]; then
                auto_fill_env
            fi
        fi
        # .env가 이미 있어도 DOCKER_GID는 현재 시스템에 맞게 갱신
        ensure_docker_gid
        return
    fi

    if [ ! -f "$ENV_EXAMPLE" ]; then
        log_error ".env.example 파일 없음: $ENV_EXAMPLE"
        exit 1
    fi

    cp "$ENV_EXAMPLE" "$ENV_FILE"

    echo ""
    read -p "도메인 (예: ctf.university.ac.kr, 없으면 Enter): " -r domain
    domain="${domain:-localhost}"
    read -p "관리자 이메일: " -r admin_email
    admin_email="${admin_email:-admin@ctf.local}"
    read -p "관리자 유저네임 (기본: admin): " -r admin_username
    admin_username="${admin_username:-admin}"
    read -sp "관리자 비밀번호 (8자 이상, 필수): " -r admin_password
    echo ""
    if [ ${#admin_password} -lt 8 ]; then
        log_error "관리자 비밀번호는 8자 이상 직접 입력해야 합니다. 비밀번호를 자동 생성하거나 출력하지 않고 설치를 중단합니다."
        exit 1
    fi

    sed -i "s|SITE_DOMAIN=YOUR_DOMAIN|SITE_DOMAIN=$domain|g" "$ENV_FILE"
    sed -i "s|NEXT_PUBLIC_SITE_URL=https://YOUR_DOMAIN|NEXT_PUBLIC_SITE_URL=https://$domain|g" "$ENV_FILE"
    sed -i "s|FRONTEND_URL=http://localhost:3100|FRONTEND_URL=https://$domain|g" "$ENV_FILE"
    sed -i "s|ADMIN_EMAIL=admin@your-domain.ac.kr|ADMIN_EMAIL=$admin_email|g" "$ENV_FILE"
    sed -i "s|ADMIN_USERNAME=admin|ADMIN_USERNAME=$admin_username|g" "$ENV_FILE"
    sed -i "s|ADMIN_PASSWORD=CHANGE_ME_admin_password|ADMIN_PASSWORD=$admin_password|g" "$ENV_FILE"
    sed -i "s|SMTP_FROM=noreply@your-domain.ac.kr|SMTP_FROM=noreply@$domain|g" "$ENV_FILE"
    sed -i "s|ENVIRONMENT=development|ENVIRONMENT=production|g" "$ENV_FILE"

    # 프로덕션은 RS256 (PEM 파일) — 개발용 HS256 기본값 제거
    sed -i "s|^JWT_ALGORITHM=HS256|JWT_ALGORITHM=RS256|" "$ENV_FILE"
    sed -i "s|^JWT_SECRET_KEY=.*|JWT_SECRET_KEY=|" "$ENV_FILE"

    auto_fill_env
    ensure_docker_gid

    log_ok ".env 설정 완료"
}

# Docker GID 자동 감지 — .env가 이미 있어도 시스템마다 다르므로 항상 갱신
ensure_docker_gid() {
    local docker_gid
    docker_gid=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo 999)
    if grep -q "^DOCKER_GID=" "$ENV_FILE"; then
        sed -i "s|^DOCKER_GID=.*|DOCKER_GID=$docker_gid|" "$ENV_FILE"
    else
        echo "DOCKER_GID=$docker_gid" >> "$ENV_FILE"
    fi
    log_ok "DOCKER_GID=$docker_gid 설정"
}

auto_fill_env() {
    local changes=0
    for key in POSTGRES_PASSWORD AUTH_DB_PASSWORD LEARN_DB_PASSWORD COMMUNITY_DB_PASSWORD REDIS_PASSWORD MINIO_ROOT_PASSWORD INTERNAL_API_KEY CTFD_SECRET_KEY; do
        local current
        current=$(grep "^${key}=" "$ENV_FILE" | cut -d= -f2- | tr -d ' "')
        if [[ "$current" == CHANGE_ME* || "$current" == changeme* ]]; then
            sed -i "s|^${key}=.*|${key}=$(gen_password)|" "$ENV_FILE"
            changes=$((changes + 1))
        fi
    done
    [ $changes -gt 0 ] && log_ok "$changes개 비밀번호 자동 생성"
}

validate_env() {
    log_step ".env 검증"
    [ ! -f "$ENV_FILE" ] && { log_error ".env 없음. 'deploy.sh install' 먼저 실행"; exit 1; }

    local errors=0
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$key" ]] && continue
        value="${value%\"}"; value="${value#\"}"
        case "$value" in
            CHANGE_ME*|YOUR_DOMAIN|changeme*) log_error "기본값: $key"; errors=$((errors + 1)) ;;
        esac
    done < "$ENV_FILE"

    [ $errors -gt 0 ] && { log_error "$errors개 환경변수 수정 필요: nano $ENV_FILE"; exit 1; }
    log_ok ".env 검증 통과"
}

# ──── 4단계: JWT RS256 키 ────
ensure_jwt_keys() {
    local key
    local assignment
    local source_path

    [ -r "$ENV_FILE" ] || { log_error "소스 폴더 JWT 키 생성은 보안상 중단되었습니다. 프로덕션 외부 JWT 키 설정 파일이 필요합니다."; return 1; }

    for key in JWT_SECRET_KEY_SOURCE JWT_PUBLIC_KEY_SOURCE; do
        assignment="$(grep -E "^[[:space:]]*${key}=[^[:space:]]+" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
        if [ -z "$assignment" ]; then
            log_error "소스 폴더 JWT 키 생성은 보안상 중단되었습니다. 프로덕션 외부 JWT 키 경로 설정이 필요합니다."
            return 1
        fi
        source_path="${assignment#*=}"
        source_path="${source_path%\"}"
        source_path="${source_path#\"}"
        if [ ! -f "$source_path" ] || [ -L "$source_path" ]; then
            log_error "소스 폴더 JWT 키 생성은 보안상 중단되었습니다. 외부 JWT 키 파일을 안전하게 확인할 수 없습니다."
            return 1
        fi
    done
    return 0
}

assert_knowledge_map_v2_source_contract() {
    local graph_file="$PROJECT_DIR/frontend/src/components/learn/KnowledgeGraphMap.tsx"
    local learn_page="$PROJECT_DIR/frontend/src/app/[locale]/learn/page.tsx"
    local header_file="$PROJECT_DIR/frontend/src/components/layout/Header.tsx"
    local caddy_file="$INFRA_DIR/caddy/Caddyfile"

    if [ ! -f "$graph_file" ]; then
        log_error "V2 knowledge map source is missing; refusing deployment"
        return 1
    fi
    for endpoint in "/api/learn/knowledge-map/v2/overview" \
                    "/api/learn/knowledge-map/v2/focus" \
                    "data-knowledge-map-version=\"v2\"" \
                    "data-map-layout=\"obsidian-canvas-v2\"" \
                    "data-map-ui-revision=\"free-canvas-click-focus-v3\""; do
        if ! grep -Fq "$endpoint" "$graph_file"; then
            log_error "V2 knowledge map source contract is incomplete; refusing deployment"
            return 1
        fi
    done
    if grep -Fq "/api/learn/knowledge-map/v2/search" "$graph_file" \
        || grep -Fq "/api/learn/knowledge-map/v2/canvas" "$graph_file"; then
        log_error "검색·전체 캔버스 경로가 지식맵 UI에 남아 있어 배포를 중단합니다"
        return 1
    fi
    if grep -Fq "/api/learn/knowledge-graph" "$graph_file"; then
        log_error "legacy knowledge graph client is still referenced; refusing deployment"
        return 1
    fi
    for ui_file in "$graph_file" "$learn_page" "$header_file"; do
        if [ ! -f "$ui_file" ]; then
            log_error "current knowledge map UI source is missing; refusing deployment"
            return 1
        fi
        for legacy_copy in \
            "보안 지식 관계 지도" \
            "선택한 콘텐츠 중심의 지식 관계 지도" \
            "개요로 돌아가기" \
            "콘텐츠 검색" \
            "다음 관계 더 보기" \
            "공개 콘텐츠를 검색하지 못했습니다." \
            "배치 초기화" \
            "내 강의"; do
            if grep -Fq "$legacy_copy" "$ui_file"; then
                log_error "구형 지식맵 UI 문구가 소스에 남아 있어 배포를 중단합니다"
                return 1
            fi
        done
    done
    if [ ! -f "$caddy_file" ] || ! grep -Fq "handle /ctf/api/learn/*" "$caddy_file"; then
        log_error "Learn V2 proxy route is missing; refusing deployment"
        return 1
    fi
}

# ──── 5단계: Docker 네트워크 + /etc/hosts ────
ensure_challenge_network() {
    local net="ctf-challenges"
    if docker network inspect "$net" >/dev/null 2>&1; then return; fi
    docker network create --driver bridge --subnet 172.30.0.0/16 "$net"
    log_ok "챌린지 네트워크 생성 완료"
}

# host 모드 서비스(env-orchestrator)가 Docker 내부 호스트명을 해석할 수 있도록
# /etc/hosts에 minio 항목을 추가한다.
# presigned URL 서명에 호스트가 포함되므로 URL 리라이트 대신 DNS 해석으로 해결.
ensure_hosts_entries() {
    local SUDO
    SUDO=$(need_sudo)

    if ! grep -q "^127\.0\.0\.1.*\bminio\b" /etc/hosts 2>/dev/null; then
        echo "127.0.0.1 minio" | $SUDO tee -a /etc/hosts >/dev/null
        log_ok "/etc/hosts에 minio 항목 추가"
    else
        log_ok "/etc/hosts minio 항목 이미 존재"
    fi
}

# ──── 6단계: 방화벽 ────
setup_firewall() {
    log_step "방화벽 설정"
    local SUDO
    SUDO=$(need_sudo)

    if command -v ufw &>/dev/null; then
        setup_ufw "$SUDO"
    elif command -v iptables &>/dev/null; then
        setup_iptables "$SUDO"
    else
        log_warn "방화벽 도구 없음. 수동 설정 필요:"
        log_info "  포트: 22, 80, 443, 10000-60000 TCP"
    fi
}

setup_ufw() {
    local SUDO=$1
    local opened=0
    for port in "22/tcp" "80/tcp" "443/tcp" "10000:60000/tcp"; do
        if ! $SUDO ufw status 2>/dev/null | grep -q "$port.*ALLOW"; then
            $SUDO ufw allow "$port" >/dev/null 2>&1 && opened=$((opened + 1))
        fi
    done
    # ufw 활성화
    if ! $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
        read -p "ufw 활성화? (Y/n) " -r
        [[ ! $REPLY =~ ^[Nn]$ ]] && $SUDO ufw --force enable >/dev/null 2>&1
    fi
    log_ok "ufw 방화벽 설정 완료 (SSH + HTTP + HTTPS + 챌린지 포트)"
}

setup_iptables() {
    local SUDO=$1
    for port in 80 443; do
        $SUDO iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || \
        $SUDO iptables -A INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null
    done
    $SUDO iptables -C INPUT -p tcp --dport 10000:60000 -j ACCEPT 2>/dev/null || \
    $SUDO iptables -A INPUT -p tcp --dport 10000:60000 -j ACCEPT 2>/dev/null

    if command -v netfilter-persistent &>/dev/null; then
        $SUDO netfilter-persistent save 2>/dev/null
        log_ok "iptables 규칙 영구 저장"
    else
        log_warn "iptables-persistent 미설치 — 재부팅 시 규칙 초기화됨"
    fi
    log_ok "iptables 방화벽 설정 완료"
}

# ──── 헬스체크 ────
healthcheck() {
    log_step "헬스체크"
    command -v curl &>/dev/null || { log_warn "curl 없음 — 건너뜀"; return; }

    local max_wait=90 interval=5 all_ok=true

    local endpoints=(
        "auth-service:http://localhost:8020/health"
        "learn-service:http://localhost:8100/health"
        "community-service:http://localhost:8300/health"
        "env-orchestrator:http://localhost:8200/health"
        "frontend:http://localhost:3100/ctf/ko"
    )

    for ep in "${endpoints[@]}"; do
        local svc="${ep%%:*}" url="${ep#*:}" elapsed=0
        while [ $elapsed -lt $max_wait ]; do
            if curl -sf --max-time 5 "$url" >/dev/null 2>&1; then
                log_ok "$svc"; break
            fi
            sleep $interval; elapsed=$((elapsed + interval))
        done
        [ $elapsed -ge $max_wait ] && { log_error "$svc — 응답 없음"; all_ok=false; }
    done

    local domain
    domain=$(grep -E "^SITE_DOMAIN=" "$ENV_FILE" 2>/dev/null | cut -d= -f2 | tr -d ' "')
    if [ -n "$domain" ] && [ "$domain" != "localhost" ]; then
        curl -sf --max-time 10 "https://$domain" >/dev/null 2>&1 && \
            log_ok "HTTPS ($domain)" || \
            log_warn "HTTPS ($domain) — 접속 불가"
    fi

    [ "$all_ok" = true ] && { echo ""; log_ok "모든 서비스 정상"; }
}

# ──── 요약 출력 ────
print_summary() {
    local domain admin_email
    domain=$(grep -E "^SITE_DOMAIN=" "$ENV_FILE" 2>/dev/null | cut -d= -f2 | tr -d ' "')
    admin_email=$(grep -E "^ADMIN_EMAIL=" "$ENV_FILE" 2>/dev/null | cut -d= -f2 | tr -d ' "')

    echo ""
    echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}${BOLD}       CTF 학습 플랫폼 — 배포 완료!${NC}"
    echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  ${BOLD}접속 URL:${NC}       https://$domain"
    echo -e "  ${BOLD}관리자:${NC}         $admin_email"
    echo -e "  ${BOLD}챌린지 접속:${NC}    host:port 직접 (포트 10000-60000)"
    echo ""
    echo -e "  ${BOLD}서비스 관리:${NC}"
    echo "    상태:     bash infra/scripts/deploy.sh status"
    echo "    로그:     bash infra/scripts/deploy.sh logs [서비스명]"
    echo "    업데이트: bash infra/scripts/deploy.sh update"
    echo "    중지:     bash infra/scripts/deploy.sh stop"
    echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
}

# ──── 명령어 처리 ────
case "${1:-help}" in
    install)
        echo -e "${CYAN}${BOLD}"
        echo "  ╔═══════════════════════════════════════════════╗"
        echo "  ║   CTF 학습 플랫폼 — 원클릭 설치 (Linux)      ║"
        echo "  ╚═══════════════════════════════════════════════╝"
        echo -e "${NC}"

        # Windows에서 복사한 경우 CRLF → LF 변환
        log_info "줄바꿈 변환 (CRLF → LF)..."
        find "$PROJECT_DIR" -type f \( \
            -name "*.sh" -o -name "*.py" -o -name "*.go" -o -name "*.yml" -o -name "*.yaml" \
            -o -name "*.env" -o -name ".env" -o -name ".env.*" \
            -o -name "Dockerfile" -o -name "Caddyfile" \
            -o -name "*.toml" -o -name "*.ini" -o -name "*.cfg" \
            -o -name "*.txt" -o -name "*.json" -o -name "*.mod" -o -name "*.sum" \
        \) -not -path "*/node_modules/*" -not -path "*/.next/*" -exec sed -i 's/\r$//' {} +
        # .env 파일은 infra 디렉토리에도 별도 확인
        [ -f "$ENV_FILE" ] && sed -i 's/\r$//' "$ENV_FILE"
        log_ok "줄바꿈 변환 완료"

        install_system_packages
        check_prerequisites
        setup_env
        validate_env
        ensure_jwt_keys
        assert_knowledge_map_v2_source_contract
        ensure_challenge_network
        ensure_hosts_entries
        setup_firewall

        log_step "Docker 이미지 빌드"
        $COMPOSE_CMD build

        log_step "서비스 시작"
        $COMPOSE_CMD up -d --build

        sleep 5
        healthcheck
        print_summary
        ;;

    start)
        # 구버전 checkout에서도 업데이트 자체가 막히지 않도록, 새 소스를
        # 받은 뒤 V2 그래프 계약을 검증한다. 검증 실패 시 빌드/기동은
        # 수행하지 않고 여기서 중단한다.
        validate_env; ensure_jwt_keys; assert_knowledge_map_v2_source_contract; ensure_challenge_network
        $COMPOSE_CMD up -d --build
        sleep 3; healthcheck
        ;;

    stop)
        $COMPOSE_CMD down
        log_ok "모든 서비스 중지"
        ;;

    restart)
        $COMPOSE_CMD down; sleep 2
        validate_env; ensure_jwt_keys; assert_knowledge_map_v2_source_contract; ensure_challenge_network
        $COMPOSE_CMD up -d --build
        sleep 3; healthcheck
        ;;

    status)
        $COMPOSE_CMD ps; echo ""; healthcheck
        ;;

    update)
        # 구버전 checkout에서도 업데이트 자체가 막히지 않도록, 새 소스를
        # 받은 뒤 V2 그래프 계약을 검증한다. 검증 실패 시 빌드/기동은
        # 수행하지 않고 여기서 중단한다.
        validate_env; ensure_jwt_keys; ensure_challenge_network
        log_info "최신 코드 pull..."
        cd "$PROJECT_DIR" && git pull
        assert_knowledge_map_v2_source_contract
        log_info "Docker 이미지 재빌드..."
        $COMPOSE_CMD build
        $COMPOSE_CMD up -d --build
        sleep 5; healthcheck; log_ok "업데이트 완료"
        ;;

    logs)
        shift; $COMPOSE_CMD logs -f --tail=100 "${@:-}"
        ;;

    firewall)
        setup_firewall
        ;;

    help|*)
        echo -e "${BOLD}CTF 학습 플랫폼 — 프로덕션 배포 (Linux)${NC}"
        echo ""
        echo "사용법: bash $0 [명령어]"
        echo ""
        echo -e "${BOLD}명령어:${NC}"
        echo "  install   원클릭 설치 (패키지 + 환경설정 + 빌드 + 방화벽)"
        echo "  start     서비스 시작"
        echo "  stop      서비스 중지"
        echo "  restart   재시작"
        echo "  status    상태 + 헬스체크"
        echo "  update    git pull + 재빌드 + 재시작"
        echo "  logs      로그 (예: deploy.sh logs ctf-auth)"
        echo "  firewall  방화벽 규칙만 설정"
        echo ""
        echo -e "${BOLD}빠른 시작:${NC}"
        echo "  git clone <repo> && cd CTF_Web"
        echo "  bash infra/scripts/deploy.sh install"
        ;;
esac
