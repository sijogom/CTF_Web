#!/bin/bash
# 의존성 취약점 검사 스크립트
# CI/CD에서도 사용 가능

set -euo pipefail

echo "=== 의존성 보안 감사 ==="
echo "시작: $(date)"
echo ""

FAIL_COUNT=0

# --- Python (pip audit) ---
# 서브셸 ( ) 로 디렉토리 이동을 격리하여 cd 실패 시 안전성 확보
echo "--- Python 의존성 (pip audit) ---"
for service in auth learn community; do
  if [ -d "services/$service" ]; then
    echo "  스캔: services/$service"
    (
      cd "services/$service"
      pip audit --strict 2>&1 || { echo "  [WARN] $service pip audit 실패"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
    )
  fi
done
echo ""

# --- Node.js (npm audit) ---
echo "--- Node.js 의존성 (npm audit) ---"
if [ -d "frontend" ]; then
  (
    cd frontend
    npm audit --audit-level=high 2>&1 || { echo "  [WARN] npm audit 실패"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
  )
fi
echo ""

# --- Go (govulncheck) ---
echo "--- Go 의존성 (govulncheck) ---"
if [ -d "services/env-orchestrator" ]; then
  (
    cd services/env-orchestrator
    if command -v govulncheck &> /dev/null; then
      govulncheck ./... 2>&1 || { echo "  [WARN] govulncheck 실패"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
    else
      echo "  [SKIP] govulncheck 미설치"
    fi
  )
fi
echo ""

# --- Docker 이미지 (Trivy) ---
echo "--- Docker 이미지 (Trivy) ---"
if command -v trivy &> /dev/null; then
  for image in ctf-auth ctf-frontend ctf-learn ctf-community ctf-orchestrator; do
    echo "  스캔: $image"
    trivy image --severity HIGH,CRITICAL "$image" 2>&1 || echo "  [WARN] $image 스캔 실패"
  done
else
  echo "  [SKIP] Trivy 미설치 (설치: https://trivy.dev)"
fi
echo ""

echo "=== 감사 완료 (경고: $FAIL_COUNT) ==="
