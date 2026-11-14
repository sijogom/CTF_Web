#!/bin/bash
# ═══════════════════════════════════════════════════════════
# CTF 학습 플랫폼 — 프로덕션 배포 스크립트 (Windows WSL2)
#
# 원클릭 설치: bash infra/scripts/deploy-wsl.sh install
# 서비스 관리: bash infra/scripts/deploy-wsl.sh [start|stop|status|update|logs]
#
# 대상 환경: Windows 10/11 + WSL2 + Docker Desktop
# Linux 네이티브: deploy.sh 사용
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
FRONTEND_DIR="$PROJECT_DIR/frontend"

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

is_wsl() { grep -qi microsoft /proc/version 2>/dev/null; }

# ──── 1단계: WSL2 환경 확인 ────
check_wsl_environment() {
    log_step "WSL2 환경 확인"

    if ! is_wsl; then
        log_error "이 스크립트는 WSL2 환경 전용입니다."
        log_info "Linux 네이티브는 deploy.sh를 사용하세요."
        exit 1
    fi
    log_ok "WSL2 환경 확인"

    # Docker 확인 (Docker Desktop이 WSL2에 docker CLI를 제공)
    if command -v docker &>/dev/null; then
        log_ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'available')"
    else
        log_error "Docker를 찾을 수 없습니다"
        log_info "Docker Desktop을 설치하고 WSL2 통합을 활성화하세요:"
        log_info "  https://docs.docker.com/desktop/wsl/"
        exit 1
    fi

    if docker compose version &>/dev/null; then
        log_ok "Docker Compose $(docker compose version --short 2>/dev/null)"
    else
        log_error "Docker Compose 미설치"
        exit 1
    fi

    # Docker 데몬 접근
    if docker info &>/dev/null; then
        log_ok "Docker 데몬 접근 가능"
    else
        log_error "Docker Desktop이 실행 중인지 확인하세요"
        exit 1
    fi

    # OpenSSL
    if command -v openssl &>/dev/null; then
        log_ok "OpenSSL"
    else
        log_info "OpenSSL 설치 중..."
        sudo apt-get update -qq && sudo apt-get install -y -qq openssl 2>/dev/null
    fi

    # curl
    if command -v curl &>/dev/null; then
        log_ok "curl"
    else
        sudo apt-get install -y -qq curl 2>/dev/null
    fi

    # git
    if command -v git &>/dev/null; then
        log_ok "git"
    else
        sudo apt-get install -y -qq git 2>/dev/null
    fi

    # Node.js (프론트엔드 빌드용 — WSL2에서는 Windows Node 사용)
    if command -v node &>/dev/null || cmd.exe /c "node --version" 2>/dev/null | grep -q "v"; then
        log_ok "Node.js"
    else
        log_warn "Node.js를 찾을 수 없습니다 — 프론트엔드 빌드에 필요합니다"
        log_info "  Windows: https://nodejs.org (LTS 설치)"
        log_info "  또는 WSL2: sudo apt install nodejs npm"
    fi

    # Python (백엔드용)
    if command -v python3 &>/dev/null; then
        log_ok "Python $(python3 --version 2>&1 | awk '{print $2}')"
    else
        log_warn "Python3를 찾을 수 없습니다 — 개발 모드 서비스에 필요합니다"
        log_info "  sudo apt install python3 python3-pip python3-venv"
    fi

    # pip 패키지 확인 (백엔드 서비스용)
    log_info "Python 패키지 확인..."
    local services=("auth" "learn" "community")
    for svc in "${services[@]}"; do
        local req_file="$PROJECT_DIR/services/$svc/requirements.txt"
        if [ -f "$req_file" ]; then
            log_ok "services/$svc/requirements.txt 존재"
        else
            log_warn "services/$svc/requirements.txt 없음"
        fi
    done
}

# ──── 2단계: pip 패키지 설치 (WSL2 개발 모드) ────
install_python_packages() {
    log_step "Python 패키지 설치"

    if ! command -v python3 &>/dev/null; then
        log_warn "Python3 미설치 — 건너뜀"
        return
    fi

    # 시스템 패키지 (cryptography, Pillow 빌드에 필요)
    sudo apt-get install -y -qq libffi-dev libssl-dev python3-dev 2>/dev/null || true

    local services=("auth" "learn" "community")
    for svc in "${services[@]}"; do
        local req_file="$PROJECT_DIR/services/$svc/requirements.txt"
        if [ -f "$req_file" ]; then
            log_info "$svc 패키지 설치 중..."
            pip3 install -q -r "$req_file" 2>/dev/null && log_ok "$svc 패키지 완료" || log_warn "$svc 일부 패키지 실패"
        fi
    done
}

# ──── 3단계: npm 패키지 설치 (Windows Node.js) ────
install_frontend_packages() {
    log_step "프론트엔드 패키지 설치"

    local win_dir
    win_dir=$(wslpath -w "$FRONTEND_DIR" 2>/dev/null)

    if [ -z "$win_dir" ]; then
        log_warn "wslpath 변환 실패 — 건너뜀"
        return
    fi

    # Windows에서 npm ci 실행
    if cmd.exe /c "where npm" &>/dev/null 2>&1; then
        log_info "npm ci 실행 중 (Windows Node.js)..."
        powershell.exe -Command "cd '$win_dir'; npm ci" 2>/dev/null && log_ok "npm 패키지 설치 완료" || log_warn "npm ci 실패 — npm install로 재시도"
        powershell.exe -Command "cd '$win_dir'; npm install" 2>/dev/null || true
    else
        log_warn "Windows npm을 찾을 수 없습니다 — 수동 설치 필요"
        log_info "  프론트엔드 폴더에서 npm install 실행"
    fi
}

# ──── .env 설정 (deploy.sh와 동일) ────
setup_env() {
    log_step ".env 설정"

    if [ -f "$ENV_FILE" ]; then
        log_ok ".env 파일 존재"
        if grep -q "CHANGE_ME\|YOUR_DOMAIN" "$ENV_FILE"; then
            log_warn "기본값이 남아있습니다"
            read -p "자동으로 안전한 비밀번호를 생성할까요? (Y/n) " -r
            [[ ! $REPLY =~ ^[Nn]$ ]] && auto_fill_env
        fi
        return
    fi

    [ ! -f "$ENV_EXAMPLE" ] && { log_error ".env.example 없음"; exit 1; }
    cp "$ENV_EXAMPLE" "$ENV_FILE"

    echo ""
    read -p "도메인 (예: example.com, 없으면 Enter): " -r domain
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

    auto_fill_env
    log_ok ".env 설정 완료"
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
    [ ! -f "$ENV_FILE" ] && { log_error ".env 없음"; exit 1; }
    local errors=0
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$key" ]] && continue
        value="${value%\"}"; value="${value#\"}"
        case "$value" in
            CHANGE_ME*|YOUR_DOMAIN|changeme*) log_error "기본값: $key"; errors=$((errors + 1)) ;;
        esac
    done < "$ENV_FILE"
    [ $errors -gt 0 ] && { log_error "$errors개 환경변수 수정 필요"; exit 1; }
    log_ok ".env 검증 통과"
}

# ──── JWT RS256 키 ────
ensure_jwt_keys() {
    log_error "소스 폴더 JWT 키 생성은 보안상 중단되었습니다. ctfctl init-jwt-secrets로 외부 key source를 준비한 뒤 새 배포 절차를 사용하세요."
    return 1
}

# ──── Docker 네트워크 ────
ensure_challenge_network() {
    local net="ctf-challenges"
    docker network inspect "$net" >/dev/null 2>&1 && return
    docker network create --driver bridge --subnet 172.30.0.0/16 "$net"
    log_ok "챌린지 네트워크 생성"
}

# ──── WSL2 portproxy 설정 ────
setup_portproxy() {
    log_step "Windows portproxy 설정"

    if ! command -v cmd.exe &>/dev/null; then
        log_warn "Windows 명령어 접근 불가"; return
    fi

    local wsl_ip
    wsl_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -z "$wsl_ip" ] && { log_error "WSL IP 감지 실패"; return 1; }

    local current_rules expected_rule
    current_rules=$(cmd.exe /c "netsh interface portproxy show all" 2>/dev/null | tr -d '\r')
    expected_rule="127.0.0.1.*80.*${wsl_ip}.*8080"

    if echo "$current_rules" | grep -Eqi "$expected_rule"; then
        log_ok "개발용 portproxy 정상 (Windows 127.0.0.1:80 → WSL $wsl_ip:8080)"
        return
    fi

    if echo "$current_rules" | grep -Eqi "127.0.0.1.*80|0.0.0.0.*80"; then
        log_error "기존 Windows 80 portproxy 규칙을 덮어쓰지 않습니다. 현재 규칙을 확인한 뒤 명시적으로 정리하세요."
        return 1
    fi

    log_info "개발용 localhost HTTP portproxy 추가 필요 (Windows 127.0.0.1:80 → WSL $wsl_ip:8080) — 관리자 권한 요청..."
    local cmds="netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=80 connectaddress=$wsl_ip connectport=8080"

    powershell.exe -Command "Start-Process -Verb RunAs -FilePath 'cmd.exe' -ArgumentList '/c $cmds' -Wait" 2>/dev/null
    sleep 1

    local verify
    verify=$(cmd.exe /c "netsh interface portproxy show all" 2>/dev/null | tr -d '\r')
    if echo "$verify" | grep -Eqi "$expected_rule"; then
        log_ok "개발용 portproxy 추가 완료"
    else
        log_error "portproxy 설정 실패 — 기존 규칙을 확인한 뒤 수동으로 복구하세요"
        return 1
    fi
}

# ──── server.sh로 전체 서비스 시작 (개발 모드 호환) ────
start_with_server_sh() {
    log_step "서비스 시작 (server.sh)"
    cd "$INFRA_DIR"
    bash scripts/server.sh start
}

# ──── 헬스체크 ────
healthcheck() {
    log_step "헬스체크"
    command -v curl &>/dev/null || { log_warn "curl 없음"; return; }

    # 포트 상태 파일에서 실제 포트 읽기
    local auth_port learn_port comm_port frontend_port
    if [ -f /tmp/ctf-port-state ]; then
        source /tmp/ctf-port-state 2>/dev/null
        auth_port=${PORTS_auth:-8020}
        learn_port=${PORTS_learn:-8100}
        comm_port=${PORTS_community:-8300}
        frontend_port=${PORTS_frontend:-3100}
    else
        auth_port=8020; learn_port=8100; comm_port=8300; frontend_port=3100
    fi

    local all_ok=true max_wait=60 interval=5

    local endpoints=(
        "auth-service:http://localhost:$auth_port/health"
        "learn-service:http://localhost:$learn_port/health"
        "community-service:http://localhost:$comm_port/health"
        "env-orchestrator:http://localhost:8200/health"
        "frontend:http://localhost:$frontend_port"
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

    [ "$all_ok" = true ] && { echo ""; log_ok "모든 서비스 정상"; }
}

# ──── 요약 ────
print_summary() {
    local domain
    domain=$(grep -E "^SITE_DOMAIN=" "$ENV_FILE" 2>/dev/null | cut -d= -f2 | tr -d ' "')
    local admin_email
    admin_email=$(grep -E "^ADMIN_EMAIL=" "$ENV_FILE" 2>/dev/null | cut -d= -f2 | tr -d ' "')

    echo ""
    echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}${BOLD}       CTF 학습 플랫폼 — 설치 완료! (WSL2)${NC}"
    echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  ${BOLD}접속 URL:${NC}       http://localhost (Windows loopback portproxy 사용 시)"
    echo -e "  ${BOLD}관리자:${NC}         $admin_email"
    echo -e "  ${BOLD}챌린지 접속:${NC}    Caddy 프록시 경유 (proxy 모드)"
    echo ""
    echo -e "  ${BOLD}서비스 관리:${NC}"
    echo "    상태:     bash infra/scripts/server.sh status"
    echo "    시작:     bash infra/scripts/server.sh start"
    echo "    중지:     bash infra/scripts/server.sh stop"
    echo ""
    echo -e "  ${YELLOW}[WSL2 참고]${NC}"
    echo "    WSL 재시작 후: bash infra/scripts/server.sh start"
    echo "    portproxy 복원: restore_portproxy.bat (관리자 실행)"
    echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
}

# ──── 명령어 처리 ────
case "${1:-help}" in
    install)
        echo -e "${CYAN}${BOLD}"
        echo "  ╔═══════════════════════════════════════════════╗"
        echo "  ║   CTF 학습 플랫폼 — 원클릭 설치 (WSL2)       ║"
        echo "  ╚═══════════════════════════════════════════════╝"
        echo -e "${NC}"

        check_wsl_environment
        install_python_packages
        install_frontend_packages
        setup_env
        validate_env
        ensure_jwt_keys
        ensure_challenge_network
        setup_portproxy
        start_with_server_sh
        print_summary
        ;;

    start)
        setup_portproxy
        start_with_server_sh
        ;;

    stop)
        cd "$INFRA_DIR"
        bash scripts/server.sh stop
        ;;

    status)
        cd "$INFRA_DIR"
        bash scripts/server.sh status
        ;;

    restart)
        cd "$INFRA_DIR"
        bash scripts/server.sh restart
        ;;

    update)
        log_info "최신 코드 pull..."
        cd "$PROJECT_DIR" && git pull
        install_python_packages
        install_frontend_packages
        cd "$INFRA_DIR"
        bash scripts/server.sh restart
        log_ok "업데이트 완료"
        ;;

    logs)
        shift
        docker compose -p ctf_web logs -f --tail=100 "${@:-}" 2>/dev/null || {
            log_info "Docker 로그 없음 — 파일 로그 확인:"
            tail -50 /tmp/auth.log /tmp/learn.log /tmp/community.log 2>/dev/null || true
        }
        ;;

    portproxy)
        setup_portproxy
        ;;

    help|*)
        echo -e "${BOLD}CTF 학습 플랫폼 — 프로덕션 배포 (Windows WSL2)${NC}"
        echo ""
        echo "사용법: bash $0 [명령어]"
        echo ""
        echo -e "${BOLD}명령어:${NC}"
        echo "  install    원클릭 설치 (환경확인 + 패키지 + 설정 + 시작)"
        echo "  start      서비스 시작"
        echo "  stop       서비스 중지"
        echo "  restart    재시작"
        echo "  status     상태 확인"
        echo "  update     git pull + 패키지 + 재시작"
        echo "  logs       서비스 로그"
        echo "  portproxy  portproxy만 설정"
        echo ""
        echo -e "${BOLD}사전 요구사항:${NC}"
        echo "  - Windows 10/11 + WSL2"
        echo "  - Docker Desktop (WSL2 통합 활성화)"
        echo "  - Node.js (Windows에 설치)"
        echo ""
        echo -e "${BOLD}빠른 시작:${NC}"
        echo "  bash infra/scripts/deploy-wsl.sh install"
        ;;
esac
