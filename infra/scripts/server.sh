#!/bin/bash
# ═══════════════════════════════════════════════════════════
# CTF 학습 플랫폼 — 서버 관리 스크립트
# 사용법: bash infra/scripts/server.sh [start|stop|status|restart]
# 환경: WSL2 (Windows 프로세스도 처리)
#
# 모든 서비스 포트는 동적 배정됨:
#   - Hyper-V 예약 범위 자동 감지 및 회피
#   - 이미 사용 중인 포트 자동 회피
#   - 배정된 포트를 환경변수로 모든 의존 서비스에 전파
# ═══════════════════════════════════════════════════════════
set -e
trap 'log_fail "스크립트 실패 — server.sh status 로 현재 상태를 확인하세요."' ERR

# ──── 경로 자동 감지 ────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$(cd "$INFRA_DIR/.." && pwd)"
FRONTEND_DIR="$PROJECT_DIR/frontend"

# 포트 상태 파일 (동적 배정된 포트를 기록 — stop/status에서 참조)
PORT_STATE_FILE="/tmp/ctf-port-state"

# WSL2 게이트웨이 IP (= Windows 호스트 IP) 자동 감지
GATEWAY_IP=${FRONTEND_HOST:-$(ip route show default 2>/dev/null | awk '{print $3}')}
if [ -z "$GATEWAY_IP" ]; then
    GATEWAY_IP="localhost"
fi
export FRONTEND_HOST="$GATEWAY_IP"

# WSL2 자체 IP — Windows에서 실행되는 Next.js가 WSL 백엔드에 접근할 때 사용
WSL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -n "$WSL_IP" ]; then
    export BACKEND_HOST="$WSL_IP"
fi

# .env에서 SITE_DOMAIN, ENVIRONMENT 로드
if [ -f "$INFRA_DIR/.env" ]; then
    SITE_DOMAIN=$(grep -E '^SITE_DOMAIN=' "$INFRA_DIR/.env" 2>/dev/null | cut -d= -f2)
    ENVIRONMENT=$(grep -E '^ENVIRONMENT=' "$INFRA_DIR/.env" 2>/dev/null | cut -d= -f2)
fi
export SITE_DOMAIN="${SITE_DOMAIN:-localhost}"
ENVIRONMENT="${ENVIRONMENT:-development}"
IS_PROD=false
if [ "$ENVIRONMENT" = "production" ]; then
    IS_PROD=true
fi

# Docker 소켓 GID 자동 감지 (env-orchestrator 비루트 실행용)
export DOCKER_GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo 999)

if [ "$IS_PROD" = true ]; then
    COMPOSE_CMD="docker compose -p ctf_web -f $INFRA_DIR/docker-compose.yml -f $INFRA_DIR/docker-compose.prod.yml"
else
    COMPOSE_CMD="docker compose -p ctf_web -f $INFRA_DIR/docker-compose.dev.yml"
fi

# ──── 포트 기본 설정 ────
# 선호 포트 (사용 가능하면 이 포트 사용, 불가능하면 자동 탐색)
declare -A PORT_PREFERRED
PORT_PREFERRED[auth]=8020
PORT_PREFERRED[learn]=8100
PORT_PREFERRED[community]=8300
PORT_PREFERRED[frontend]=3100

# 서비스별 탐색 범위
declare -A PORT_RANGE_START
PORT_RANGE_START[auth]=8020
PORT_RANGE_START[learn]=8100
PORT_RANGE_START[community]=8300
PORT_RANGE_START[frontend]=3100

declare -A PORT_RANGE_END
PORT_RANGE_END[auth]=9999
PORT_RANGE_END[learn]=9999
PORT_RANGE_END[community]=9999
PORT_RANGE_END[frontend]=4200

# 예약 금지 포트 (게임 개발용)
RESERVED_PORTS="3000 8000 8188 8200"

# 실제 배정된 포트 (start 시 결정)
declare -A PORTS

# ──── 색상 ────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
log_fail() { echo -e "${RED}[FAIL]${NC} $1"; }
log_info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

# dynamic_instance 플래그 파일은 Docker daemon과 env-orchestrator가 공유하는
# 관리형 호스트 경로에만 둔다. 이 개발용 시작 경로는 디렉터리를 만들지 않으며,
# 동적 플래그 기능을 켰을 때만 아래 계약을 fail-closed로 검사한다.
DYNAMIC_FLAG_RUNTIME_ROOT="/var/lib/ctf-web"
DYNAMIC_FLAG_RUNTIME_DEFAULT="${DYNAMIC_FLAG_RUNTIME_ROOT}/dynamic-flags"
DYNAMIC_FLAG_RUNTIME_UID="1001"
DYNAMIC_FLAG_RUNTIME_GID="1001"
DYNAMIC_FLAG_COMPOSE_FILE="$INFRA_DIR/docker-compose.dynamic-flags.yml"

dynamic_flag_env_value() {
    local key="$1"
    local default_value="$2"
    local value=""
    local assignment=""

    case "$key" in
        DYNAMIC_INSTANCE_FLAGS_ENABLED) value="${DYNAMIC_INSTANCE_FLAGS_ENABLED:-}" ;;
        DYNAMIC_FLAG_RUNTIME_DIR) value="${DYNAMIC_FLAG_RUNTIME_DIR:-}" ;;
        *) return 2 ;;
    esac

    if [ -n "$value" ]; then
        printf '%s\n' "$value"
        return 0
    fi

    if [ -f "$INFRA_DIR/.env" ]; then
        assignment="$(grep -E "^[[:space:]]*${key}=" "$INFRA_DIR/.env" 2>/dev/null | tail -n 1 || true)"
        if [ -n "$assignment" ]; then
            value="${assignment#*=}"
        fi
    fi

    printf '%s\n' "${value:-$default_value}"
}

dynamic_flag_feature_is_enabled() {
    local value
    value="$(dynamic_flag_env_value "DYNAMIC_INSTANCE_FLAGS_ENABLED" "false")" || return 2

    case "$value" in
        true|1) return 0 ;;
        false|0|"") return 1 ;;
        *)
            log_fail "DYNAMIC_INSTANCE_FLAGS_ENABLED 값이 올바르지 않습니다. 값은 출력하지 않았습니다."
            return 2
            ;;
    esac
}

dynamic_flag_runtime_dir() {
    local runtime_dir
    runtime_dir="$(dynamic_flag_env_value "DYNAMIC_FLAG_RUNTIME_DIR" "$DYNAMIC_FLAG_RUNTIME_DEFAULT")" || return 1

    if [[ ! "$runtime_dir" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
        log_fail "동적 플래그 런타임 경로 형식이 올바르지 않습니다. 경로 값은 출력하지 않았습니다."
        return 1
    fi
    if [[ "$runtime_dir" != "$DYNAMIC_FLAG_RUNTIME_ROOT" && "$runtime_dir" != "$DYNAMIC_FLAG_RUNTIME_ROOT/"* ]]; then
        log_fail "동적 플래그 런타임 경로가 관리 범위를 벗어났습니다. 경로 값은 출력하지 않았습니다."
        return 1
    fi

    printf '%s\n' "$runtime_dir"
}

ensure_dynamic_flag_runtime_contract() {
    local feature_result
    local runtime_dir
    local resolved_dir
    local owner
    local group
    local mode

    if dynamic_flag_feature_is_enabled; then
        :
    else
        feature_result=$?
        if [ "$feature_result" -eq 1 ]; then
            return 0
        fi
        return "$feature_result"
    fi

    runtime_dir="$(dynamic_flag_runtime_dir)" || return 1
    if [ ! -d "$runtime_dir" ] || [ -L "$runtime_dir" ]; then
        log_fail "동적 플래그 런타임 디렉터리가 없거나 안전하지 않습니다. 자동 생성하지 않았습니다."
        return 1
    fi

    resolved_dir="$(readlink -f -- "$runtime_dir")" || {
        log_fail "동적 플래그 런타임 디렉터리를 안전하게 확인할 수 없습니다."
        return 1
    }
    if [[ "$resolved_dir" != "$DYNAMIC_FLAG_RUNTIME_ROOT" && "$resolved_dir" != "$DYNAMIC_FLAG_RUNTIME_ROOT/"* ]]; then
        log_fail "동적 플래그 런타임 디렉터리가 심볼릭 링크로 관리 범위를 벗어났습니다."
        return 1
    fi

    owner="$(stat -c '%u' -- "$resolved_dir")"
    group="$(stat -c '%g' -- "$resolved_dir")"
    mode="$(stat -c '%a' -- "$resolved_dir")"
    if [ "$owner" != "$DYNAMIC_FLAG_RUNTIME_UID" ] || [ "$group" != "$DYNAMIC_FLAG_RUNTIME_GID" ] || [ "$mode" != "700" ]; then
        log_fail "동적 플래그 런타임 디렉터리 소유자 또는 권한이 안전 계약과 다릅니다."
        return 1
    fi

    log_ok "동적 플래그 런타임 디렉터리 사전 점검 통과"
}

# ──── Hyper-V 예약 포트 감지 ────

# Windows Hyper-V 예약 포트 범위를 전역 배열로 파싱 (1회만 실행)
declare -a EXCLUDED_STARTS=()
declare -a EXCLUDED_ENDS=()

load_excluded_ports() {
    if [ ${#EXCLUDED_STARTS[@]} -gt 0 ]; then
        return  # 이미 로드됨
    fi
    if command -v cmd.exe &>/dev/null; then
        while IFS= read -r line; do
            local s=$(echo "$line" | awk '{print $1}' | tr -d ' ')
            local e=$(echo "$line" | awk '{print $2}' | tr -d ' ')
            if [[ "$s" =~ ^[0-9]+$ ]] && [[ "$e" =~ ^[0-9]+$ ]]; then
                EXCLUDED_STARTS+=("$s")
                EXCLUDED_ENDS+=("$e")
            fi
        done < <(cmd.exe /c "netsh interface ipv4 show excludedportrange protocol=tcp" 2>/dev/null | tr -d '\r')
    fi
}

# 포트가 Hyper-V 예약 범위에 걸리는지 확인
is_port_excluded() {
    local port=$1
    local i
    for i in "${!EXCLUDED_STARTS[@]}"; do
        if [ "$port" -ge "${EXCLUDED_STARTS[$i]}" ] && [ "$port" -le "${EXCLUDED_ENDS[$i]}" ]; then
            return 0
        fi
    done
    return 1
}

# 포트가 예약 금지 목록에 있는지 확인
is_port_reserved() {
    local port=$1
    for r in $RESERVED_PORTS; do
        if [ "$port" -eq "$r" ]; then
            return 0
        fi
    done
    return 1
}

# ──── 포트 관리 (WSL + Windows 양쪽 처리) ────

is_port_in_use() {
    local port=$1
    if lsof -i :$port -t &>/dev/null; then
        return 0
    fi
    # Linux production hosts often do not ship lsof or cmd.exe.  Docker's
    # published ports are still visible through the kernel socket table.
    if command -v ss &>/dev/null; then
        if ss -ltnH "( sport = :$port )" 2>/dev/null | grep -q .; then
            return 0
        fi
    fi
    if command -v cmd.exe &>/dev/null; then
        if cmd.exe /c "netstat -ano 2>nul" 2>/dev/null | grep -q ":${port} .*LISTENING"; then
            return 0
        fi
    fi
    return 1
}

kill_port() {
    local port=$1
    local killed=false

    local pids=$(lsof -i :$port -t 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "$pids" | xargs kill -15 2>/dev/null || true
        sleep 2
        # 아직 살아있으면 SIGKILL
        local remaining=$(lsof -i :$port -t 2>/dev/null)
        if [ -n "$remaining" ]; then
            echo "$remaining" | xargs kill -9 2>/dev/null || true
        fi
        killed=true
    fi

    if command -v cmd.exe &>/dev/null; then
        local win_pids=$(cmd.exe /c "netstat -ano 2>nul" 2>/dev/null \
            | grep ":${port} " | grep "LISTENING" \
            | awk '{print $NF}' | tr -d '\r' | sort -u)
        for win_pid in $win_pids; do
            if [ -n "$win_pid" ] && [ "$win_pid" != "0" ]; then
                cmd.exe /c "taskkill /PID $win_pid /T /F >nul 2>&1" 2>/dev/null || true
                killed=true
            fi
        done
    fi

    if [ "$killed" = true ]; then
        sleep 2
    fi

    if is_port_in_use $port; then
        log_fail "포트 $port 해제 실패 — 수동 확인 필요"
        return 1
    fi

    if [ "$killed" = true ]; then
        log_info "포트 $port 프로세스 종료 완료"
    fi
    return 0
}

# ──── 동적 포트 배정 ────

# 이번 세션에서 이미 배정된 포트 목록 (중복 방지)
declare -a ASSIGNED_PORTS=()

is_port_already_assigned() {
    local port=$1
    for p in "${ASSIGNED_PORTS[@]}"; do
        if [ "$port" -eq "$p" ]; then
            return 0
        fi
    done
    return 1
}

# 서비스 이름으로 사용 가능한 포트를 찾아 PORTS[name]에 저장
find_available_port() {
    local name=$1
    local preferred=${PORT_PREFERRED[$name]}
    local range_start=${PORT_RANGE_START[$name]}
    local range_end=${PORT_RANGE_END[$name]}

    load_excluded_ports

    # 선호 포트 먼저 시도
    if ! is_port_excluded $preferred && ! is_port_reserved $preferred \
       && ! is_port_already_assigned $preferred && ! is_port_in_use $preferred 2>/dev/null; then
        PORTS[$name]=$preferred
        ASSIGNED_PORTS+=($preferred)
        return 0
    fi

    # 범위 내에서 순차 탐색
    local port=$range_start
    while [ $port -le $range_end ]; do
        if is_port_reserved $port || is_port_already_assigned $port; then
            port=$((port + 1))
            continue
        fi
        if ! is_port_excluded $port && ! is_port_in_use $port 2>/dev/null; then
            PORTS[$name]=$port
            ASSIGNED_PORTS+=($port)
            return 0
        fi
        port=$((port + 1))
    done

    # 모두 실패
    log_fail "$name: 포트 ${range_start}-${range_end} 범위에서 사용 가능한 포트 없음"
    PORTS[$name]=$preferred
    return 1
}

# 모든 서비스 포트를 한 번에 배정하고 로그 출력
resolve_all_ports() {
    log_info "포트 배정 중 (Hyper-V 예약 범위 자동 회피)..."
    local all_ok=true

    for name in auth learn community frontend; do
        find_available_port $name || all_ok=false
        local preferred=${PORT_PREFERRED[$name]}
        local assigned=${PORTS[$name]}
        if [ "$assigned" != "$preferred" ]; then
            log_info "$name: 선호 포트 $preferred 사용 불가 → $assigned 배정"
        fi
    done

    echo ""
    log_info "배정된 포트: auth=${PORTS[auth]}, learn=${PORTS[learn]}, community=${PORTS[community]}, frontend=${PORTS[frontend]}"

    # 포트 상태 파일에 저장 (stop/status에서 참조)
    save_port_state

    if [ "$all_ok" = false ]; then
        return 1
    fi
    return 0
}

# 포트 상태 파일 저장/로드
save_port_state() {
    cat > "$PORT_STATE_FILE" <<EOF
AUTH_PORT=${PORTS[auth]}
LEARN_PORT=${PORTS[learn]}
COMMUNITY_PORT=${PORTS[community]}
FRONTEND_PORT=${PORTS[frontend]}
EOF
}

load_port_state() {
    if [ -f "$PORT_STATE_FILE" ]; then
        source "$PORT_STATE_FILE"
        PORTS[auth]=${AUTH_PORT:-${PORT_PREFERRED[auth]}}
        PORTS[learn]=${LEARN_PORT:-${PORT_PREFERRED[learn]}}
        PORTS[community]=${COMMUNITY_PORT:-${PORT_PREFERRED[community]}}
        PORTS[frontend]=${FRONTEND_PORT:-${PORT_PREFERRED[frontend]}}
        return 0
    fi

    # 상태 파일이 없으면 기본값 사용하되 실제 실행 중인 포트 탐색
    for name in auth learn community frontend; do
        PORTS[$name]=${PORT_PREFERRED[$name]}
    done
    # 실제 실행 중인 서비스가 기본 포트에 있는지 확인, 아니면 범위 내 탐색
    for name in auth learn community; do
        local preferred=${PORT_PREFERRED[$name]}
        if curl -s -m 2 "http://localhost:$preferred/health" > /dev/null 2>&1; then
            PORTS[$name]=$preferred
        else
            local start=${PORT_RANGE_START[$name]}
            local end=${PORT_RANGE_END[$name]}
            local port=$start
            while [ $port -le $end ]; do
                if curl -s -m 2 "http://localhost:$port/health" > /dev/null 2>&1; then
                    PORTS[$name]=$port
                    break
                fi
                port=$((port + 1))
            done
        fi
    done
    # 프론트엔드도 탐색 (health 대신 root 응답으로 판별)
    local fe_preferred=${PORT_PREFERRED[frontend]}
    local fe_code
    fe_code=$(curl -s -o /dev/null -w '%{http_code}' -m 2 "http://localhost:$fe_preferred/" 2>/dev/null || echo "000")
    if [ "$fe_code" != "000" ] && [ "$fe_code" != "000" ]; then
        PORTS[frontend]=$fe_preferred
    else
        local fe_start=${PORT_RANGE_START[frontend]}
        local fe_end=${PORT_RANGE_END[frontend]}
        local fe_port=$fe_start
        while [ $fe_port -le $fe_end ]; do
            fe_code=$(curl -s -o /dev/null -w '%{http_code}' -m 1 "http://localhost:$fe_port/" 2>/dev/null || echo "000")
            if [ "$fe_code" != "000" ]; then
                PORTS[frontend]=$fe_port
                break
            fi
            fe_port=$((fe_port + 1))
        done
    fi
    return 0
}

# ──── portproxy 확인·추가 (WSL 개발용 로컬 HTTP) ────

ensure_portproxy() {
    if [ -z "$WSL_IP" ]; then
        log_fail "WSL IP를 감지할 수 없어 portproxy 설정 불가"
        return 1
    fi

    local current_rules
    current_rules=$(cmd.exe /c "netsh interface portproxy show all" 2>/dev/null | tr -d '\r')

    local port80_ok=false

    # 개발 Caddy는 WSL의 :8080에서 HTTP로 수신한다. 기존의 80/443 →
    # WSL 80/443 전달은 이 리스너와 일치하지 않았고, 다른 portproxy 규칙을
    # 전체 reset하면 사용자 시스템의 다른 전달 규칙을 지울 수 있다.
    if echo "$current_rules" | grep -Eq "127\.0\.0\.1[[:space:]]+80[[:space:]]+${WSL_IP}[[:space:]]+8080"; then
        port80_ok=true
    fi

    if [ "$port80_ok" = true ]; then
        log_ok "portproxy 정상 (Windows 127.0.0.1:80 → WSL $WSL_IP:8080)"
        return 0
    fi

    if echo "$current_rules" | grep -Eq "[[:space:]]80[[:space:]]"; then
        log_fail "기존 Windows portproxy 80 규칙을 덮어쓰지 않습니다. 규칙을 확인한 뒤 명시적으로 정리하세요."
        return 1
    fi

    log_info "개발용 localhost HTTP portproxy 추가 필요 — 관리자 권한 요청..."

    local netsh_cmds="netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=80 connectaddress=$WSL_IP connectport=8080"

    powershell.exe -Command "Start-Process -Verb RunAs -FilePath 'cmd.exe' -ArgumentList '/c $netsh_cmds' -Wait" 2>/dev/null

    sleep 1
    local verify
    verify=$(cmd.exe /c "netsh interface portproxy show all" 2>/dev/null | tr -d '\r')

    if echo "$verify" | grep -Eq "127\.0\.0\.1[[:space:]]+80[[:space:]]+${WSL_IP}[[:space:]]+8080"; then
        log_ok "portproxy 추가 완료 (Windows 127.0.0.1:80 → WSL $WSL_IP:8080)"
    else
        log_fail "portproxy 추가 실패 — 규칙을 확인한 뒤 다시 시도하세요"
        return 1
    fi
}

# ──── 헬스체크 ────

wait_for_health() {
    local port=$1
    local name=$2
    local max_wait=${3:-15}
    local count=0
    while [ $count -lt $max_wait ]; do
        if curl -s -m 3 "http://localhost:$port/health" > /dev/null 2>&1; then
            log_ok "$name (포트 $port) 정상"
            return 0
        fi
        sleep 1
        count=$((count + 1))
    done
    log_fail "$name (포트 $port) 시작 실패 (${max_wait}초 타임아웃)"
    return 1
}

wait_for_frontend() {
    local port=${PORTS[frontend]}
    local max_wait=${1:-40}
    local count=0
    while [ $count -lt $max_wait ]; do
        if command -v cmd.exe &>/dev/null; then
            local win_code=$(cmd.exe /c "curl -s -m 5 -o nul -w \"%{http_code}\" http://localhost:$port" 2>/dev/null | tr -d '\r')
            if [ -n "$win_code" ] && [ "$win_code" != "000" ] && [ "$win_code" != "" ]; then
                log_ok "프론트엔드 (포트 $port) 정상"
                return 0
            fi
        fi
        sleep 2
        count=$((count + 2))
    done
    log_fail "프론트엔드 (포트 $port) 시작 실패 (${max_wait}초 타임아웃)"
    return 1
}

# ──── Docker 관리 ────

assert_knowledge_map_v2_source_contract() {
    if [ "$IS_PROD" != true ]; then
        return 0
    fi

    local graph_file="$FRONTEND_DIR/src/components/learn/KnowledgeGraphMap.tsx"
    local learn_page="$FRONTEND_DIR/src/app/[locale]/learn/page.tsx"
    local header_file="$FRONTEND_DIR/src/components/layout/Header.tsx"
    local caddy_file="$INFRA_DIR/caddy/Caddyfile"

    if [ ! -f "$graph_file" ]; then
        log_fail "V2 knowledge map source is missing; refusing production start"
        return 1
    fi
    for endpoint in "/api/learn/knowledge-map/v2/overview" \
                    "/api/learn/knowledge-map/v2/focus" \
                    "data-knowledge-map-version=\"v2\"" \
                    "data-map-layout=\"obsidian-canvas-v2\"" \
                    "data-map-ui-revision=\"free-canvas-click-focus-v3\""; do
        if ! grep -Fq "$endpoint" "$graph_file"; then
            log_fail "V2 knowledge map source contract is incomplete; refusing production start"
            return 1
        fi
    done
    if grep -Fq "/api/learn/knowledge-map/v2/search" "$graph_file" \
        || grep -Fq "/api/learn/knowledge-map/v2/canvas" "$graph_file"; then
        log_fail "검색·전체 캔버스 경로가 지식맵 UI에 남아 있어 운영 시작을 중단합니다"
        return 1
    fi
    if grep -Fq "/api/learn/knowledge-graph" "$graph_file"; then
        log_fail "legacy knowledge graph client is still referenced; refusing production start"
        return 1
    fi
    for ui_file in "$graph_file" "$learn_page" "$header_file"; do
        if [ ! -f "$ui_file" ]; then
            log_fail "current knowledge map UI source is missing; refusing production start"
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
                log_fail "구형 지식맵 UI 문구가 소스에 남아 있어 운영 시작을 중단합니다"
                return 1
            fi
        done
    done
    if [ ! -f "$caddy_file" ] || ! grep -Fq "handle /ctf/api/learn/*" "$caddy_file"; then
        log_fail "Learn V2 proxy route is missing; refusing production start"
        return 1
    fi
}

start_docker() {
    local auth_port=${PORTS[auth]}
    local learn_port=${PORTS[learn]}
    local community_port=${PORTS[community]}
    local frontend_port=${PORTS[frontend]}
    local compose_cmd="$COMPOSE_CMD"
    local feature_result

    assert_knowledge_map_v2_source_contract
    ensure_dynamic_flag_runtime_contract
    if dynamic_flag_feature_is_enabled; then
        if [ ! -f "$DYNAMIC_FLAG_COMPOSE_FILE" ]; then
            log_fail "동적 플래그 Compose 오버레이 파일이 없습니다."
            return 1
        fi
        compose_cmd="$compose_cmd -f $DYNAMIC_FLAG_COMPOSE_FILE"
    else
        feature_result=$?
        if [ "$feature_result" -ne 1 ]; then
            return "$feature_result"
        fi
    fi

    log_info "Docker 인프라 컨테이너 시작 (FRONTEND_HOST=$GATEWAY_IP)..."
    cd "$INFRA_DIR"

    # Caddy와 env-orchestrator에 동적 포트를 환경변수로 전달
    if [ "$IS_PROD" = true ]; then
        AUTH_PORT=$auth_port \
        LEARN_PORT=$learn_port \
        COMMUNITY_PORT=$community_port \
        FRONTEND_PORT=$frontend_port \
        $compose_cmd up -d --build
    else
        AUTH_PORT=$auth_port \
        LEARN_PORT=$learn_port \
        COMMUNITY_PORT=$community_port \
        FRONTEND_PORT=$frontend_port \
        $compose_cmd up -d
    fi

    log_ok "Docker 컨테이너 시작 완료"
}

stop_docker() {
    log_info "Docker 인프라 컨테이너 중지..."
    cd "$INFRA_DIR"
    $COMPOSE_CMD stop
    log_ok "Docker 컨테이너 중지 완료"
}

# ──── 백엔드 서비스 ────

start_service() {
    local name=$1
    local port=${PORTS[$name]}

    # 이미 실행 중이면 스킵
    if curl -s -m 3 "http://localhost:$port/health" > /dev/null 2>&1; then
        log_ok "$name-service 이미 실행 중 (포트 $port)"
        return 0
    fi

    # 좀비 프로세스 정리
    if is_port_in_use $port; then
        log_info "$name-service 포트 $port 좀비 프로세스 정리"
    fi
    kill_port $port || true

    # 서비스별 환경변수 구성 (동적 포트 전파)
    local env_vars="FRONTEND_PORT=${PORTS[frontend]}"
    case $name in
        auth)
            env_vars="$env_vars LEARN_SERVICE_URL=http://localhost:${PORTS[learn]} COMMUNITY_SERVICE_URL=http://localhost:${PORTS[community]} ENV_ORCHESTRATOR_URL=http://localhost:8200 FRONTEND_URL=http://localhost:${PORTS[frontend]}"
            ;;
        learn)
            env_vars="$env_vars AUTH_SERVICE_URL=http://localhost:${PORTS[auth]}"
            ;;
        community)
            env_vars="$env_vars AUTH_SERVICE_URL=http://localhost:${PORTS[auth]}"
            ;;
    esac

    cd "$PROJECT_DIR/services/$name"
    if [ "$IS_PROD" = true ]; then
        nohup env $env_vars python3 -m uvicorn app.main:app --host 0.0.0.0 --port $port \
            --no-server-header > /tmp/ctf-${name}.log 2>&1 &
    else
        nohup env $env_vars python3 -m uvicorn app.main:app --host 0.0.0.0 --port $port --reload \
            --no-server-header > /tmp/ctf-${name}.log 2>&1 &
    fi

    wait_for_health $port "$name-service"
}

stop_service() {
    local name=$1
    local port=${PORTS[$name]}
    kill_port $port || true
    log_ok "$name-service 중지 (포트 $port)"
}

# ──── 프론트엔드 (Windows 프로세스로 실행) ────

is_frontend_running() {
    local port=${PORTS[frontend]}
    if ! is_port_in_use $port; then
        return 1
    fi
    if command -v cmd.exe &>/dev/null; then
        local win_code=$(cmd.exe /c "curl -s -m 5 -o nul -w \"%{http_code}\" http://localhost:$port" 2>/dev/null | tr -d '\r')
        if [ -n "$win_code" ] && [ "$win_code" != "000" ] && [ "$win_code" != "" ]; then
            return 0
        fi
    fi
    local http_code=$(curl -s -m 5 -o /dev/null -w "%{http_code}" "http://localhost:$port" 2>/dev/null || echo "000")
    if [ "$http_code" != "000" ]; then
        return 0
    fi
    return 1
}

start_frontend() {
    local port=${PORTS[frontend]}

    # 이미 실행 중이면 스킵
    if is_frontend_running; then
        log_ok "프론트엔드 이미 실행 중 (포트 $port)"
        return 0
    fi

    # 좀비 프로세스 정리
    if is_port_in_use $port; then
        log_info "포트 $port 좀비 프로세스 정리"
        kill_port $port || true
    fi

    rm -f "$FRONTEND_DIR/.next/dev/lock"

    local win_dir=$(wslpath -w "$FRONTEND_DIR")
    log_info "프론트엔드 시작 중 (포트 $port, BACKEND_HOST=${BACKEND_HOST:-localhost})..."

    # 모든 백엔드 포트를 환경변수로 전달
    local env_set="set BACKEND_HOST=${BACKEND_HOST:-localhost}"
    env_set="$env_set&& set AUTH_PORT=${PORTS[auth]}"
    env_set="$env_set&& set LEARN_PORT=${PORTS[learn]}"
    env_set="$env_set&& set COMMUNITY_PORT=${PORTS[community]}"
    env_set="$env_set&& set ENV_ORCH_PORT=8200"
    env_set="$env_set&& set MINIO_PORT=9000"

    if [ "$IS_PROD" = true ]; then
        # 프로덕션: Compose의 Caddy/Env 프록시 계약과 동일하게 상대 경로 proxy만
        # 사용한다. 인스턴스 HostPort는 loopback에만 바인딩되므로 direct URL이나
        # 외부 포트 개방을 전제로 하면 안 된다. Next middleware가 basePath 요청을
        # 내부 재요청할 때 Windows의 `localhost`가 IPv6(::1)로 해석될 수 있으므로,
        # host-mode 예비 경로는 0.0.0.0에 바인딩하고 공개 진입점은 계속 Caddy로
        # 제한한다(동적 인스턴스 포트는 여전히 proxy 경로만 사용).
        env_set="$env_set&& set NEXT_PUBLIC_ENV_ACCESS_MODE=proxy"
        powershell.exe -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c cd /d ${win_dir} && ${env_set}&& set NEXT_PUBLIC_SITE_URL=https://${SITE_DOMAIN}&& set NEXT_PUBLIC_ENV_PROXY_BASE=&& npm run build && npx next start --port ${port} --hostname 0.0.0.0' -WindowStyle Hidden" 2>/dev/null
        wait_for_frontend 120
    else
        # 개발: proxy 모드 (Caddy 경유, WSL2 동적 포트 외부 노출 불가)
        env_set="$env_set&& set NEXT_PUBLIC_ENV_ACCESS_MODE=proxy"
        env_set="$env_set&& set NEXT_PUBLIC_ENV_PROXY_BASE=http://${BACKEND_HOST:-localhost}:8200"
        powershell.exe -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c cd /d ${win_dir} && ${env_set}&& npx next dev --port ${port} --hostname 0.0.0.0' -WindowStyle Hidden" 2>/dev/null
        wait_for_frontend 40
    fi
}

stop_frontend() {
    local port=${PORTS[frontend]}
    kill_port $port || true
    rm -f "$FRONTEND_DIR/.next/dev/lock"
    log_ok "프론트엔드 중지"
}

# ──── JWT RS256 키 관리 (프로덕션 전용) ────

ensure_jwt_keys() {
    local env_file="$INFRA_DIR/.env"
    local key
    local assignment
    local source_path

    if [ ! -r "$env_file" ]; then
        log_fail "소스 폴더 JWT 키 생성은 보안상 중단되었습니다. 프로덕션 외부 JWT 키 설정 파일이 필요합니다."
        return 1
    fi

    for key in JWT_SECRET_KEY_SOURCE JWT_PUBLIC_KEY_SOURCE; do
        assignment="$(grep -E "^[[:space:]]*${key}=[^[:space:]]+" "$env_file" 2>/dev/null | tail -n 1 || true)"
        if [ -z "$assignment" ]; then
            log_fail "소스 폴더 JWT 키 생성은 보안상 중단되었습니다. 프로덕션 외부 JWT 키 경로 설정이 필요합니다."
            return 1
        fi
        source_path="${assignment#*=}"
        source_path="${source_path%\"}"
        source_path="${source_path#\"}"
        if [ ! -f "$source_path" ] || [ -L "$source_path" ]; then
            log_fail "소스 폴더 JWT 키 생성은 보안상 중단되었습니다. 외부 JWT 키를 안전하게 확인할 수 없습니다."
            return 1
        fi
    done

    return 0
}

# ──── 상태 확인 ────

show_status() {
    echo ""
    if [ "$IS_PROD" = true ]; then
        echo "=== CTF 플랫폼 서비스 상태 [프로덕션] ==="
    else
        echo "=== CTF 플랫폼 서비스 상태 [개발] ==="
    fi
    echo ""

    echo "── Docker 컨테이너 ──"
    cd "$INFRA_DIR"
    for container in $($COMPOSE_CMD ps --format '{{.Name}}' 2>/dev/null); do
        local state=$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || echo "not found")
        if [ "$state" = "running" ]; then
            log_ok "$container"
        else
            log_fail "$container ($state)"
        fi
    done

    echo ""
    echo "── 백엔드 서비스 ──"
    for name in auth learn community; do
        local port=${PORTS[$name]}
        if curl -s -m 3 "http://localhost:$port/health" > /dev/null 2>&1; then
            log_ok "$name-service (포트 $port)"
        else
            log_fail "$name-service (포트 $port)"
        fi
    done

    if curl -s -m 3 "http://localhost:8200/health" > /dev/null 2>&1; then
        log_ok "env-orchestrator (포트 8200)"
    else
        log_fail "env-orchestrator (포트 8200)"
    fi

    echo ""
    echo "── 프론트엔드 ──"
    if is_frontend_running; then
        log_ok "Next.js (포트 ${PORTS[frontend]})"
    else
        log_fail "Next.js (포트 ${PORTS[frontend]})"
    fi

    echo ""
    echo "── 네트워크 ──"
    log_info "WSL 게이트웨이 IP: $GATEWAY_IP"
    log_info "WSL 자체 IP (BACKEND_HOST): ${WSL_IP:-localhost}"

    if [ "$IS_PROD" != true ] && command -v cmd.exe &>/dev/null && [ -n "$WSL_IP" ]; then
        local pp_rules
        pp_rules=$(cmd.exe /c "netsh interface portproxy show all" 2>/dev/null | tr -d '\r')
        if echo "$pp_rules" | grep -Eq "127\.0\.0\.1[[:space:]]+80[[:space:]]+${WSL_IP}[[:space:]]+8080"; then
            log_ok "portproxy (Windows 127.0.0.1:80 → WSL $WSL_IP:8080)"
        else
            log_fail "portproxy 미설정 또는 IP 불일치 — WSL :8080으로 직접 접속하거나 규칙을 명시적으로 확인하세요"
        fi
    fi
    echo ""
}

# ──── 메인 ────
case "${1:-status}" in
    start)
        if [ "$IS_PROD" = true ]; then
            echo "=== CTF 플랫폼 컨테이너 시작 [프로덕션 모드] ==="
            echo "    도메인: ${SITE_DOMAIN}"
            ensure_jwt_keys

            # 프로덕션 Compose는 auth/learn/community/frontend까지 모두 컨테이너로
            # 실행한다. 이 뒤에 WSL/Windows 직접 프로세스를 시작하거나 포트를
            # 정리하면 초기화 중인 컨테이너와 충돌할 수 있으므로 분리한다.
            echo ""
            resolve_all_ports
            echo ""
            start_docker
            echo ""
            show_status
            exit 0
        else
            echo "=== CTF 플랫폼 전체 시작 [개발 모드] ==="
        fi
        ensure_portproxy || true
        echo ""
        resolve_all_ports
        echo ""
        start_docker
        echo ""
        for name in auth learn community; do
            start_service $name
        done
        echo ""
        start_frontend
        echo ""
        show_status
        ;;
    stop)
        echo "=== CTF 플랫폼 전체 중지 ==="
        load_port_state
        if [ "$IS_PROD" = true ]; then
            # 프로덕션에서는 Docker Compose만 관리한다. 포트 점유 프로세스를
            # 일괄 종료하지 않아 다른 호스트 작업을 중단시키지 않는다.
            stop_docker
            rm -f "$PORT_STATE_FILE"
            echo ""
            show_status
            exit 0
        fi
        stop_frontend
        for name in auth learn community; do
            stop_service $name
        done
        stop_docker
        rm -f "$PORT_STATE_FILE"
        echo ""
        show_status
        ;;
    restart)
        echo "=== CTF 플랫폼 전체 재시작 ==="
        load_port_state
        if [ "$IS_PROD" = true ]; then
            stop_docker
            sleep 2
            ensure_jwt_keys
            resolve_all_ports
            echo ""
            start_docker
            echo ""
            show_status
            exit 0
        fi
        stop_frontend
        for name in auth learn community; do
            stop_service $name
        done
        stop_docker
        sleep 2
        resolve_all_ports
        echo ""
        start_docker
        echo ""
        for name in auth learn community; do
            start_service $name
        done
        echo ""
        start_frontend
        echo ""
        show_status
        ;;
    status)
        load_port_state
        show_status
        ;;
    *)
        echo "사용법: bash infra/scripts/server.sh [start|stop|status|restart]"
        exit 1
        ;;
esac
