#!/usr/bin/env bash
# =============================================================================
# provision_production_server.sh
#
# Run on a fresh Ubuntu 22.04 / 24.04 LTS production server as root or sudo.
# Installs: Docker, Docker Compose v2, Nginx, Certbot, Fail2ban, ufw, NTP.
# Creates non-root service user `ccall`.
# Does NOT configure the application — that is done in deploy_production.sh.
#
# Usage:
#   chmod +x provision_production_server.sh
#   sudo bash provision_production_server.sh
# =============================================================================
set -euo pipefail

CCALL_USER=ccall
CCALL_HOME=/opt/ccall

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
fatal()   { echo -e "${RED}[FATAL]${NC} $*"; exit 1; }

# --- Verify OS ---
. /etc/os-release
if [[ "$ID" != "ubuntu" ]]; then
    fatal "This script requires Ubuntu. Found: $ID $VERSION_ID"
fi
info "OS: $PRETTY_NAME"

# --- Verify not accidentally on staging ---
HOSTNAME_VAL=$(hostname)
if echo "$HOSTNAME_VAL" | grep -qi "staging\|stage\|stg"; then
    fatal "Hostname $HOSTNAME_VAL looks like a staging server. Abort."
fi

# ── 1. System update ──────────────────────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    curl gnupg lsb-release ca-certificates \
    ufw fail2ban \
    chrony \
    htop iotop net-tools dnsutils \
    openssl jq

# ── 2. Timezone and NTP ───────────────────────────────────────────────────────
info "Setting timezone to UTC..."
timedatectl set-timezone UTC
systemctl enable --now chrony
chronyc makestep
info "Time: $(date -u)"

# ── 3. Automatic security updates ────────────────────────────────────────────
info "Enabling unattended-upgrades..."
apt-get install -y -qq unattended-upgrades
cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

# ── 4. SSH hardening ──────────────────────────────────────────────────────────
info "Hardening SSH..."
# Backup sshd_config
cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%Y%m%d)
cat >> /etc/ssh/sshd_config << 'EOF'

# C-Call IVR hardening
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys
X11Forwarding no
AllowTcpForwarding no
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
sshd -t && systemctl reload sshd
info "SSH hardened. Root login: NO. Password auth: NO."
warn "IMPORTANT: Ensure your SSH key is in authorized_keys BEFORE closing this session."

# ── 5. Firewall (ufw) ─────────────────────────────────────────────────────────
info "Configuring firewall..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
# SSH — allow from anywhere initially; tighten to specific IPs manually
ufw allow ssh
ufw allow 80/tcp comment "HTTP (Certbot + redirect)"
ufw allow 443/tcp comment "HTTPS"
# SIP/RTP NAT/media ports (allow outbound; open inbound UDP RTP if carrier requires symmetric firewall allow)
ufw allow 10000:20000/udp comment "RTP media range (SIP trunk media)"
# Explicitly deny ports that must never be public
ufw deny 5432/tcp comment "PostgreSQL — internal only"
ufw deny 6379/tcp comment "Redis — internal only"
ufw deny 9090/tcp comment "Prometheus — internal only"
ufw deny 9093/tcp comment "Alertmanager — internal only"
ufw deny 5000/tcp comment "Gunicorn — Nginx proxies this"
ufw --force enable
ufw status numbered
info "Firewall active."

# ── 6. Fail2ban ────────────────────────────────────────────────────────────────
info "Configuring Fail2ban..."
cat > /etc/fail2ban/jail.d/ccall.conf << 'EOF'
[sshd]
enabled = true
maxretry = 5
bantime  = 3600
findtime = 600

[nginx-http-auth]
enabled = true
EOF
systemctl enable --now fail2ban
info "Fail2ban active."

# ── 7. Non-root service user ───────────────────────────────────────────────────
info "Creating service user: $CCALL_USER..."
if ! id "$CCALL_USER" &>/dev/null; then
    useradd --system --create-home --home-dir "$CCALL_HOME" \
            --shell /bin/bash --groups docker "$CCALL_USER" 2>/dev/null || true
    # If docker group doesn't exist yet, add after Docker install
fi
mkdir -p "$CCALL_HOME"
chown "$CCALL_USER:$CCALL_USER" "$CCALL_HOME"
chmod 750 "$CCALL_HOME"

# ── 8. Docker Engine ──────────────────────────────────────────────────────────
info "Installing Docker Engine..."
if ! command -v docker &>/dev/null; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] \
        https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
# Add ccall user to docker group
usermod -aG docker "$CCALL_USER"
systemctl enable --now docker
docker --version
docker compose version
info "Docker installed."

# ── 9. Nginx ──────────────────────────────────────────────────────────────────
info "Installing Nginx..."
apt-get install -y -qq nginx
systemctl enable nginx
# Remove default site
rm -f /etc/nginx/sites-enabled/default
# Create webroot for Certbot challenges
mkdir -p /var/www/certbot
chown www-data:www-data /var/www/certbot
info "Nginx installed."

# ── 10. Certbot ───────────────────────────────────────────────────────────────
info "Installing Certbot..."
apt-get install -y -qq certbot python3-certbot-nginx
# Auto-renewal timer
systemctl enable --now certbot.timer
info "Certbot installed. Certificate will be issued after DNS is live (run deploy_production.sh)."

# ── 11. ffmpeg ────────────────────────────────────────────────────────────────
info "Installing ffmpeg..."
apt-get install -y -qq ffmpeg
ffmpeg -version | head -1

# ── 12. Kernel hardening (sysctl) ────────────────────────────────────────────
info "Applying kernel hardening..."
cat > /etc/sysctl.d/99-ccall-hardening.conf << 'EOF'
# Prevent SYN flood
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_max_syn_backlog = 2048
# Disable IP forwarding (not a router)
net.ipv4.ip_forward = 0
# Ignore ICMP broadcasts
net.ipv4.icmp_echo_ignore_broadcasts = 1
# Log martian packets
net.ipv4.conf.all.log_martians = 1
# Disable source routing
net.ipv4.conf.all.accept_source_route = 0
# Disable ICMP redirect acceptance
net.ipv4.conf.all.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
# Enable exec-shield equivalent
kernel.randomize_va_space = 2
# Limit dmesg access
kernel.dmesg_restrict = 1
EOF
sysctl --system -q
info "Kernel hardening applied."

# ── 13. Audit summary ─────────────────────────────────────────────────────────
info "=== PROVISIONING COMPLETE ==="
echo ""
echo "Server: $(hostname) ($(hostname -I | awk '{print $1}'))"
echo "OS: $(. /etc/os-release && echo "$PRETTY_NAME")"
echo "Kernel: $(uname -r)"
echo "CPUs: $(nproc)"
echo "RAM: $(free -h | awk '/Mem:/{print $2}')"
echo "Disk: $(df -h / | awk 'NR==2{print $2 " total, " $4 " free"}')"
echo "Docker: $(docker --version)"
echo "Compose: $(docker compose version)"
echo "Nginx: $(nginx -v 2>&1)"
echo "Certbot: $(certbot --version)"
echo "UFW: $(ufw status | head -1)"
echo "Fail2ban: $(systemctl is-active fail2ban)"
echo "Time: $(date -u) ($(timedatectl | grep 'Time zone' | awk '{print $3}'))"
echo ""
warn "NEXT STEPS:"
echo "  1. Set DNS A record for app.ccall.com.bd → $(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
echo "  2. Copy application files to $CCALL_HOME"
echo "  3. Create .env.production (chmod 600)"
echo "  4. Run: python scripts/validate_production_secrets.py --env .env.production"
echo "  5. Run: sudo bash scripts/deploy_production.sh"
