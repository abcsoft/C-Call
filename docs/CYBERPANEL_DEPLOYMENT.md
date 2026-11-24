# C-Call V2 — CyberPanel (OpenLiteSpeed / Nginx) + systemd Deployment Guide

This guide supports Linux VPS/EC2 hosts in **Singapore, USA, Bangladesh**, or any region without hardcoded geographic dependencies.

## 1. Systemd Services

Create `/etc/systemd/system/ccall-web.service`:
```ini
[Unit]
Description=C-Call V2 Gunicorn Web
After=network.target postgresql.service redis.service

[Service]
User=ccall
Group=ccall
WorkingDirectory=/opt/ccall
EnvironmentFile=/opt/ccall/.env
Environment="ENABLE_SCHEDULER=false" "ENABLE_CALL_WORKER=false" "ENABLE_CALL_MONITOR=false"
ExecStart=/opt/ccall/.venv/bin/gunicorn --config gunicorn.conf.py run:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Create `/etc/systemd/system/ccall-scheduler.service` (singleton, `ENABLE_SCHEDULER=true`, `python -m app.commands.run_scheduler`) and `/etc/systemd/system/ccall-worker.service` (`ENABLE_CALL_WORKER=true`, `python -m app.commands.run_call_worker`).

## 2. CyberPanel Reverse Proxy (OpenLiteSpeed / Nginx)

1. Create website in CyberPanel with Let's Encrypt SSL enabled.
2. Add an External App (`Web Server` -> `127.0.0.1:5000`) and Rewrite/Context rule proxying `/` to `127.0.0.1:5000` (preserving `Host`, `X-Forwarded-For`, `X-Forwarded-Proto`).
3. Ensure `/health/live` and `/health/ready` return HTTP 200.

## 3. Cloud SIP / RTP NAT Configuration (EC2 / VPS)

Set in `/opt/ccall/.env`:
```ini
SIP_TRANSPORT=udp
SIP_BIND_IP=0.0.0.0
RTP_BIND_IP=0.0.0.0
SIP_ADVERTISED_IP=<PUBLIC_IPV4_OF_HOST>
RTP_ADVERTISED_IP=<PUBLIC_IPV4_OF_HOST>
RTP_PORT_MIN=10000
RTP_PORT_MAX=20000
```
Open UDP `10000-20000` (and UDP `5060` if receiving inbound SIP) in the cloud security group / firewall.
