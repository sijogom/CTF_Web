#!/bin/bash
# 통합 보안 테스트 스크립트
# 사용법: ./scripts/security-test.sh [BASE_URL]

set -euo pipefail

BASE_URL="${1:-http://localhost}"
PASS=0
FAIL=0

check() {
  local name="$1"
  local result="$2"
  if [ "$result" = "PASS" ]; then
    echo "[PASS] $name"
    PASS=$((PASS + 1))
  else
    echo "[FAIL] $name"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== 보안 테스트 시작 ==="
echo "대상: $BASE_URL"
echo ""

# --- 1. 보안 헤더 검증 ---
echo "--- 1. 보안 헤더 ---"
HEADERS=$(curl -sI "$BASE_URL" 2>/dev/null || echo "")

echo "$HEADERS" | grep -qi "x-content-type-options" && check "X-Content-Type-Options" "PASS" || check "X-Content-Type-Options" "FAIL"
echo "$HEADERS" | grep -qi "x-frame-options" && check "X-Frame-Options" "PASS" || check "X-Frame-Options" "FAIL"
echo "$HEADERS" | grep -qi "strict-transport-security" && check "HSTS" "PASS" || check "HSTS" "FAIL"
echo "$HEADERS" | grep -qi "content-security-policy" && check "CSP" "PASS" || check "CSP" "FAIL"
echo "$HEADERS" | grep -qi "permissions-policy" && check "Permissions-Policy" "PASS" || check "Permissions-Policy" "FAIL"
echo ""

# --- 2. 인증 우회 검증 ---
echo "--- 2. 인증 우회 ---"
for endpoint in "auth/me" "admin/users" "learn/progress/me"; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/$endpoint" 2>/dev/null || echo "000")
  if [ "$CODE" = "401" ] || [ "$CODE" = "403" ]; then
    check "$endpoint (인증 필수: $CODE)" "PASS"
  else
    check "$endpoint (예상: 401/403, 실제: $CODE)" "FAIL"
  fi
done
echo ""

# --- 3. Rate Limiting ---
echo "--- 3. Rate Limiting ---"
RATE_LIMITED=false
for i in $(seq 1 10); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/auth/login" \
    -H "Content-Type: application/json" -d '{"email":"test@test.com","password":"wrong"}' 2>/dev/null || echo "000")
  if [ "$CODE" = "429" ]; then
    RATE_LIMITED=true
    break
  fi
done
$RATE_LIMITED && check "Rate Limiting (로그인)" "PASS" || check "Rate Limiting (로그인)" "FAIL"
echo ""

# --- 4. 내부 API 차단 ---
echo "--- 4. 내부 API 차단 ---"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/internal/users" 2>/dev/null || echo "000")
if [ "$CODE" = "403" ]; then
  check "내부 API 차단 (/internal)" "PASS"
else
  check "내부 API 차단 (예상: 403, 실제: $CODE)" "FAIL"
fi
echo ""

echo "=== 결과: PASS=$PASS, FAIL=$FAIL ==="
