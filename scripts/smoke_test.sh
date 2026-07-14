#!/usr/bin/env bash
# scripts/smoke_test.sh — post-deployment production smoke tests
#
# Safe: uses test API key, test tenant, never real customer data.
# Exit 0 = all checks passed. Exit 1 = failure (block deploy).
#
# Required env:
#   SMOKE_BASE_URL     — e.g. https://app.ccall.com.bd
#   SMOKE_ADMIN_EMAIL  — test admin login (NOT production admin)
#   SMOKE_ADMIN_PASS   — test admin password
#   SMOKE_API_KEY      — test tenant API key (active subscription)
#   SMOKE_EXPIRED_KEY  — test tenant API key (expired subscription)
#   DATABASE_URL       — read-only integrity checks
#
set -euo pipefail
BASE="${SMOKE_BASE_URL:-https://app.ccall.com.bd}"
PASS=0
FAIL=0
LOG_PREFIX="[SMOKE $(date -u +%Y%m%dT%H%M%SZ)]"

check() {
    local name="$1" result="$2" expected="$3"
    if [[ "$result" == *"$expected"* ]]; then
        echo "$LOG_PREFIX  PASS  $name"
        ((PASS++)) || true
    else
        echo "$LOG_PREFIX  FAIL  $name (got: $result, expected: $expected)"
        ((FAIL++)) || true
    fi
}

http_status() {
    curl -s -o /dev/null -w "%{http_code}" "$@"
}

echo "$LOG_PREFIX Starting smoke tests against $BASE"

# 1. HTTPS loads
STATUS=$(http_status "$BASE/health/live")
check "https_live_endpoint" "$STATUS" "200"

# 2. Readiness
STATUS=$(http_status "$BASE/health/ready")
check "health_ready" "$STATUS" "200"

# 3. TLS redirect (http → https)
if command -v curl >/dev/null 2>&1; then
    REDIR=$(curl -s -o /dev/null -w "%{redirect_url}" "http://${BASE#https://}/health/live" 2>/dev/null || echo "")
    check "http_redirects_to_https" "$REDIR" "https://"
fi

# 4. Login page loads
STATUS=$(http_status "$BASE/auth/login")
check "login_page_loads" "$STATUS" "200"

# 5. API: active subscription → 200
if [[ -n "${SMOKE_API_KEY:-}" ]]; then
    STATUS=$(http_status -X POST "$BASE/api/v1/orders" \
        -H "X-API-Key: $SMOKE_API_KEY" \
        -H "Content-Type: application/json" \
        -d '{"order_id":"SMOKE-TEST-001","phone":"01700000099","customer_name":"Smoke Test","amount":"100"}')
    check "api_active_order_201" "$STATUS" "20"   # 200 or 201

    # Idempotency: same order_id again → 200
    STATUS=$(http_status -X POST "$BASE/api/v1/orders" \
        -H "X-API-Key: $SMOKE_API_KEY" \
        -H "Content-Type: application/json" \
        -d '{"order_id":"SMOKE-TEST-001","phone":"01700000099","customer_name":"Smoke Test","amount":"100"}')
    check "api_idempotency_200" "$STATUS" "200"
fi

# 6. API: expired subscription → 402
if [[ -n "${SMOKE_EXPIRED_KEY:-}" ]]; then
    STATUS=$(http_status -X POST "$BASE/api/v1/orders" \
        -H "X-API-Key: $SMOKE_EXPIRED_KEY" \
        -H "Content-Type: application/json" \
        -d '{"order_id":"SMOKE-EXPIRED-001","phone":"01700000099","customer_name":"Expired","amount":"100"}')
    check "api_expired_402" "$STATUS" "402"
fi

# 7. Security headers present
HEADERS=$(curl -sI "$BASE/health/live")
check "hsts_header" "$HEADERS" "Strict-Transport-Security"
check "x_content_type" "$HEADERS" "X-Content-Type-Options"
check "x_frame_options" "$HEADERS" "X-Frame-Options"

# 8. Debug mode off (no Werkzeug debug banner in HTML)
BODY=$(curl -s "$BASE/auth/login")
if echo "$BODY" | grep -qi "werkzeug\|debugger\|traceback"; then
    echo "$LOG_PREFIX  FAIL  debug_mode_off (Werkzeug debug visible)"
    ((FAIL++)) || true
else
    echo "$LOG_PREFIX  PASS  debug_mode_off"
    ((PASS++)) || true
fi

# 9. .env not accessible
STATUS=$(http_status "$BASE/.env")
if [[ "$STATUS" == "404" || "$STATUS" == "403" ]]; then
    echo "$LOG_PREFIX  PASS  env_file_blocked ($STATUS)"
    ((PASS++)) || true
else
    echo "$LOG_PREFIX  FAIL  env_file_blocked (got $STATUS)"
    ((FAIL++)) || true
fi

# 10. No BTEST_ tenants in production
if [[ -n "${DATABASE_URL:-}" ]]; then
    BTEST=$(psql "$DATABASE_URL" --tuples-only --quiet --command \
        "SELECT COUNT(*) FROM tenants WHERE business_name LIKE 'BTEST_%'" 2>/dev/null | xargs)
    if [[ "$BTEST" == "0" ]]; then
        echo "$LOG_PREFIX  PASS  no_btest_tenants"
        ((PASS++)) || true
    else
        echo "$LOG_PREFIX  FAIL  no_btest_tenants (found $BTEST)"
        ((FAIL++)) || true
    fi
fi

echo ""
echo "$LOG_PREFIX ─────────────────────────────────────────────"
echo "$LOG_PREFIX Results: PASS=$PASS FAIL=$FAIL"
echo "$LOG_PREFIX ─────────────────────────────────────────────"

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
