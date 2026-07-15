#!/usr/bin/env bash
# =============================================================================
# audit_production_host.sh
#
# Read-only audit of the production host.
# Returns the 30-item checklist required by Step 9 Phase 1.
# Run as root or sudo. Makes NO changes.
#
# Usage:
#   bash scripts/audit_production_host.sh 2>&1 | tee /tmp/host_audit_$(date +%Y%m%d).txt
# =============================================================================
set -uo pipefail

PAD=40

row() { printf "  %-${PAD}s %s\n" "$1" "$2"; }

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║          C-Call IVR — Production Host Audit              ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo "  Audit time: $(date -u)"
echo ""

echo "── System ───────────────────────────────────────────────────"
row "1.  Linux distribution"  "$(. /etc/os-release && echo "$PRETTY_NAME")"
row "2.  Kernel"              "$(uname -r)"
row "3.  CPU cores"           "$(nproc) logical ($(grep 'physical id' /proc/cpuinfo | sort -u | wc -l) physical)"
row "4.  RAM"                 "$(free -h | awk '/Mem:/{print $2 " total, " $7 " available"}')"
row "5.  Disk total/free"     "$(df -h / | awk 'NR==2{print $2 " total, " $4 " free (" $5 " used)"}')"
row "6.  Swap"                "$(free -h | awk '/Swap:/{print $2 " total, " $4 " free"}' | grep -v '0B' || echo "none")"
row "7.  Public IP"           "$(curl -s --max-time 5 ifconfig.me 2>/dev/null || echo "unavailable")"
row "8.  Private IP"          "$(hostname -I | awk '{print $1}')"
row "9.  Hostname"            "$(hostname)"
row "10. Current user"        "$(whoami) (uid=$(id -u))"

echo ""
echo "── Docker ───────────────────────────────────────────────────"
if command -v docker &>/dev/null; then
    row "11. Docker version"     "$(docker --version 2>/dev/null)"
    row "12. Docker Compose"     "$(docker compose version 2>/dev/null)"
else
    row "11. Docker version"     "NOT INSTALLED"
    row "12. Docker Compose"     "NOT INSTALLED"
fi

echo ""
echo "── Firewall and security ─────────────────────────────────────"
row "13. Firewall status"     "$(ufw status 2>/dev/null | head -1 || iptables -L -n 2>/dev/null | head -1 || echo "unknown")"
row "14. Open ports"          ""
ss -tlnp 2>/dev/null | awk 'NR>1{printf "      %-24s %s\n", $4, $6}' || netstat -tlnp 2>/dev/null | awk 'NR>2{print "      "$4, $7}'

row "15. SSH config"          ""
grep -E "^(PermitRootLogin|PasswordAuthentication|PubkeyAuthentication|MaxAuthTries)" \
    /etc/ssh/sshd_config 2>/dev/null | while read line; do echo "      $line"; done || echo "      cannot read"

row "16. Root login"          "$(grep -E '^PermitRootLogin' /etc/ssh/sshd_config 2>/dev/null || echo 'not set (check default)')"
row "17. Password auth"       "$(grep -E '^PasswordAuthentication' /etc/ssh/sshd_config 2>/dev/null || echo 'not set (check default)')"
row "18. Fail2ban"            "$(systemctl is-active fail2ban 2>/dev/null || echo 'not installed')"
row "19. Auto security updates" "$(systemctl is-enabled unattended-upgrades 2>/dev/null || echo 'not configured')"

echo ""
echo "── Running services ─────────────────────────────────────────"
row "20. Active services"     ""
systemctl list-units --type=service --state=active --no-legend 2>/dev/null \
    | awk '{print "      "$1}' | grep -v '@' | head -20

echo ""
echo "── Existing software ─────────────────────────────────────────"
row "21. Web server"          "$(nginx -v 2>&1 || apache2 -v 2>&1 | head -1 || echo "none")"
row "22. Existing databases"  "$(pg_lsclusters 2>/dev/null | tail -n+2 || psql --version 2>/dev/null | head -1 || echo "none")"
row "23. Existing Redis"      "$(redis-cli --version 2>/dev/null || echo "none")"
row "24. Existing TLS certs"  "$(ls /etc/letsencrypt/live/ 2>/dev/null | tr '\n' ' ' || echo "none")"
row "25. Existing app files"  "$(ls /opt/ccall/ 2>/dev/null | head -5 | tr '\n' ' ' || echo "none")"
row "26. Existing backups"    "$(ls /opt/ccall/backups/ 2>/dev/null | head -3 || echo "none")"
row "27. Existing deployment" "$(docker ps --format '{{.Names}}' 2>/dev/null | tr '\n' ' ' || echo "no containers")"
row "28. Existing monitoring" "$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E 'prometheus|grafana|alertmanager' | tr '\n' ' ' || echo "none")"

echo ""
echo "── Time ─────────────────────────────────────────────────────"
row "29. Timezone"            "$(timedatectl 2>/dev/null | grep 'Time zone' | awk '{print $3}' || date +%Z)"
row "30. NTP sync"            "$(timedatectl 2>/dev/null | grep 'synchronized' || chronyc tracking 2>/dev/null | head -3 || echo "unknown")"

echo ""
echo "── Minimum requirements check ────────────────────────────────"
CPUS=$(nproc)
RAM_GB=$(free -g | awk '/Mem:/{print $2}')
DISK_GB=$(df -BG / | awk 'NR==2{gsub("G",""); print $4}')

[[ $CPUS -ge 2 ]]  && echo "  ✓ CPUs: $CPUS (>= 2)" || echo "  ✗ CPUs: $CPUS (need >= 2)"
[[ $RAM_GB -ge 3 ]] && echo "  ✓ RAM: ${RAM_GB}GB (>= 3GB)" || echo "  ✗ RAM: ${RAM_GB}GB (need >= 4GB recommended)"
[[ $DISK_GB -ge 20 ]] && echo "  ✓ Free disk: ${DISK_GB}GB (>= 20GB)" || echo "  ✗ Free disk: ${DISK_GB}GB (need more)"
command -v docker &>/dev/null && echo "  ✓ Docker installed" || echo "  ✗ Docker not installed"
docker compose version &>/dev/null && echo "  ✓ Docker Compose v2 installed" || echo "  ✗ Docker Compose v2 not installed"

# Check ports 80 and 443
for PORT in 80 443; do
    if ss -tlnp | grep -q ":$PORT "; then
        PROC=$(ss -tlnp | grep ":$PORT " | awk '{print $6}')
        echo "  ! Port $PORT: in use by $PROC"
    else
        echo "  ✓ Port $PORT: available"
    fi
done

echo ""
echo "Audit complete: $(date -u)"
