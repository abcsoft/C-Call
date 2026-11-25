#!/usr/bin/env bash
# =============================================================================
# deploy_production.sh
#
# Run on the production server AFTER provision_production_server.sh.
# Performs: TLS issuance, DB migration, service start, health checks.
#
# Prerequisites:
#   - DNS A record for app.ccall.com.bd is live and propagated
#   - .env.production exists and passes validate_production_secrets.py
#   - Docker image is available (pulled or built locally)
#
# Usage (as ccall user or sudo):
#   chmod +x scripts/deploy_production.sh
#   bash scripts/deploy_production.sh
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fatal() { echo -e "${RED}[FATAL]${NC} $*"; exit 1; }
step()  { echo -e "\n${BLUE}══════════════════════════════════════════════${NC}"; \
          echo -e "${BLUE}  $*${NC}"; \
          echo -e "${BLUE}══════════════════════════════════════════════${NC}"; }

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$DEPLOY_DIR/.env.production"
DOMAIN="app.ccall.com.bd"
COMPOSE_CMD="docker compose"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.production.yml"
VERSION="${VERSION:-step8-production-ready-v6}"

cd "$DEPLOY_DIR"

# ── Pre-flight checks ────────────────────────────────────────────────────────
step "PHASE 1 — Pre-flight checks"

[[ -f "$ENV_FILE" ]] || fatal ".env.production not found at $ENV_FILE"
[[ "$(stat -c %a "$ENV_FILE")" == "600" ]] || {
    warn "Fixing .env.production permissions to 600"
    chmod 600 "$ENV_FILE"
}

info "Validating secrets..."
python3 scripts/validate_production_secrets.py --env "$ENV_FILE" \
    || fatal "Secret validation failed. Do not deploy."

# Verify no staging DB
DB_URL=$(grep '^DATABASE_URL=' "$ENV_FILE" | cut -d= -f2-)
if echo "$DB_URL" | grep -qi "staging\|test\|ccall_prod$"; then
    # Allow ccall_production but not ccall_prod (may be old name)
    if echo "$DB_URL" | grep -qi "staging\|test"; then
        fatal "DATABASE_URL contains staging/test. Abort."
    fi
fi

if [[ "${BUILD_FROM_SOURCE:-false}" == "true" ]]; then
    info "Building application image directly from local source..."
    $COMPOSE_CMD -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build
    IMAGE_DIGEST=$(docker images -q "ghcr.io/ccall/app:$VERSION" 2>/dev/null | head -1)
    [[ -z "$IMAGE_DIGEST" ]] && IMAGE_DIGEST=$(docker images -q "ghcr.io/ccall/app:latest" 2>/dev/null | head -1)
else
    info "Docker image: ghcr.io/ccall/app:$VERSION (falling back to local build if registry unavailable)"
    if docker pull "ghcr.io/ccall/app:$VERSION" 2>&1 | tail -5; then
        IMAGE_DIGEST=$(docker inspect "ghcr.io/ccall/app:$VERSION" --format '{{.Id}}' 2>/dev/null || true)
    else
        warn "Registry pull failed — building image from local source instead."
        $COMPOSE_CMD -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build
        IMAGE_DIGEST=$(docker images -q "ghcr.io/ccall/app:latest" 2>/dev/null | head -1)
    fi
fi
info "Image digest: ${IMAGE_DIGEST:-source-build}"

# ── TLS certificate ───────────────────────────────────────────────────────────
step "PHASE 2 — TLS certificate"

if [[ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
    info "Issuing TLS certificate for $DOMAIN..."
    # Verify DNS resolves to this server first
    SERVER_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
    DNS_IP=$(dig +short "$DOMAIN" A | tail -1)
    if [[ "$SERVER_IP" != "$DNS_IP" ]]; then
        warn "DNS not yet propagated. Server IP=$SERVER_IP, DNS resolves to $DNS_IP"
        warn "Wait for DNS propagation, then re-run this script."
        fatal "Cannot issue TLS certificate until DNS points to this server."
    fi
    certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos \
        -m admin@ccall.com.bd
    info "Certificate issued."
else
    info "Certificate exists. Running renewal dry-run..."
    certbot renew --dry-run --cert-name "$DOMAIN"
    info "Renewal dry-run passed."
fi

# Install Nginx config
if [[ ! -f "/etc/nginx/sites-available/ccall" ]]; then
    info "Installing Nginx config..."
    cp nginx/ccall.conf /etc/nginx/sites-available/ccall
    ln -sf /etc/nginx/sites-available/ccall /etc/nginx/sites-enabled/ccall
fi
nginx -t || fatal "Nginx config test failed"
systemctl reload nginx
info "Nginx loaded."

# ── Database startup & migration (one-off) ───────────────────────────────────
step "PHASE 3 — Start data stores & run database migration"

info "Starting postgres and redis (creates isolated internal network)..."
$COMPOSE_CMD -f "$COMPOSE_FILE" --env-file "$ENV_FILE" \
    up -d postgres redis

info "Waiting for postgres healthy..."
until docker inspect "$($COMPOSE_CMD -f "$COMPOSE_FILE" ps -q postgres 2>/dev/null | head -1)" \
    --format '{{.State.Health.Status}}' 2>/dev/null | grep -q healthy; do
    sleep 3; echo -n "."
done
echo ""

info "Checking for existing production data inside Compose network..."
EXISTING=$($COMPOSE_CMD -f "$COMPOSE_FILE" --env-file "$ENV_FILE" \
    run --rm --no-deps migrate python3 -c "
import os, psycopg
try:
    c=psycopg.connect(os.environ['DATABASE_URL'])
    cur=c.execute(\"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'\")
    n=cur.fetchone()[0]
    print(n)
    c.close()
except Exception:
    print('0')
" 2>/dev/null | tail -1 || echo "0")

if [[ "${EXISTING:-0}" -gt 0 ]]; then
    warn "Existing schema detected ($EXISTING tables). Taking pre-migration backup..."
    $COMPOSE_CMD -f "$COMPOSE_FILE" --env-file "$ENV_FILE" --profile backup \
        run --rm backup || fatal "Pre-migration backup failed. Do not continue."
fi

info "Running migrations..."
$COMPOSE_CMD -f "$COMPOSE_FILE" --env-file "$ENV_FILE" \
    run --rm migrate
info "Migration complete."

# ── Start production services ──────────────────────────────────────────────────
step "PHASE 4 — Starting application services"

info "Starting web, scheduler, call_worker..."
$COMPOSE_CMD -f "$COMPOSE_FILE" --env-file "$ENV_FILE" \
    up -d web scheduler call_worker

info "Waiting for web healthy (up to 60s)..."
ATTEMPTS=0
until docker inspect "$($COMPOSE_CMD -f "$COMPOSE_FILE" ps -q web 2>/dev/null | head -1)" \
    --format '{{.State.Health.Status}}' 2>/dev/null | grep -q healthy; do
    sleep 5; ATTEMPTS=$((ATTEMPTS+1))
    [[ $ATTEMPTS -gt 12 ]] && fatal "Web failed to become healthy after 60s"
    echo -n "."
done
echo ""
info "Web healthy."

$COMPOSE_CMD -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps

# ── Health checks ──────────────────────────────────────────────────────────────
step "PHASE 5 — Health checks"

BASE="https://$DOMAIN"
METRICS_TOKEN=$(grep '^METRICS_AUTH_TOKEN=' "$ENV_FILE" | cut -d= -f2-)

check_endpoint() {
    local url="$1" expected_code="$2" label="$3"
    local code; code=$(curl -sk -o /dev/null -w "%{http_code}" "$url")
    if [[ "$code" == "$expected_code" ]]; then
        info "✓ $label → HTTP $code"
    else
        warn "✗ $label → HTTP $code (expected $expected_code)"
    fi
}

check_endpoint "$BASE/health/live"                       200  "/health/live"
check_endpoint "$BASE/health/ready"                      200  "/health/ready"
check_endpoint "$BASE/health/details"                    401  "/health/details (no token → 401)"
check_endpoint "$BASE/metrics"                           401  "/metrics (no token → 401)"
check_endpoint "$BASE/metrics" \
    200 "/metrics (with token)" \
    -H "Authorization: Bearer $METRICS_TOKEN" 2>/dev/null || true

# Verify no .env exposure
ENV_RESP=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE/.env")
[[ "$ENV_RESP" == "404" ]] && info "✓ /.env → 404 (blocked)" \
                           || warn "✗ /.env → $ENV_RESP (should be 404)"

# Verify TLS
CERT_EXPIRY=$(echo | openssl s_client -connect "$DOMAIN:443" -servername "$DOMAIN" 2>/dev/null \
    | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
info "TLS certificate expires: $CERT_EXPIRY"

step "PHASE 6 — Deployment record"
DEPLOYED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

cat << EOF

╔══════════════════════════════════════════════════════════╗
║           C-Call IVR — Production Deployment Record     ║
╠══════════════════════════════════════════════════════════╣
║ Deployed commit   : $VERSION
║ Image digest      : ${IMAGE_DIGEST:0:24}...
║ Domain            : $DOMAIN
║ Server            : $(hostname) ($(hostname -I | awk '{print $1}'))
║ Deployed at (UTC) : $DEPLOYED_AT
║ Operator          : $(whoami)
╚══════════════════════════════════════════════════════════╝

Next steps:
  1. Create super admin: docker compose exec web flask create-admin
  2. Configure SIP credentials in admin panel
  3. Run acceptance test: PHASE 25 (controlled live order flow)
  4. Authorize real SIP call explicitly before placing it

EOF

info "Deploy complete. Do not place real calls until explicitly authorized."
