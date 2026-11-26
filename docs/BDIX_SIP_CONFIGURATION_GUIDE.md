# C-Call V2 — BDIX & SIP Trunk Configuration Guide

This guide covers BDIX/domestic network verification, IP-PBX SIP gateway configuration in C-Call V2, and carrier-restricted SIP/RTP firewall rules.

---

## 1. Verified BDIX / Domestic Connectivity Evidence (`169.40.116.43`)

Measured directly from the current Coolify server (`169.40.116.43`) to the IP-PBX trunk (`203.188.247.3:5060`):

| Metric | Observed Value |
| --- | --- |
| **Deployed Server IP** | `169.40.116.43` |
| **Server Hosting Provider / ASN** | `AS150142 Wolast Technologies` |
| **Server Physical Location** | Dhaka, Bangladesh (`BD`) |
| **SIP PBX IP & Provider** | `203.188.247.3` (`AS9832 ISN, Internet Service Provider`, Dhaka, BD) |
| **ICMP Ping RTT** | `0.853 / 0.895 / 0.941 ms` (0% packet loss) |
| **Route Hops (`tracepath`)** | 6 domestic hops (`169.40.116.1 -> 10.191.250.1 -> 192.168.11.2 -> 103.151.196.19 -> 203.188.240.9 -> 203.188.247.3`) |
| **SIP REGISTER Auth** | `401 Unauthorized (realm=asterisk)` -> `200 OK` in `1 ms` |

### How to Test BDIX / SIP Connectivity on Any New Server

Before deploying C-Call on a new VPS, verify domestic latency and UDP port 5060 reachability to your IP-PBX:

```bash
# 1. Verify server IP, ASN, and location
curl -s https://ipinfo.io/json

# 2. Check latency and route hops to your SIP PBX
ping -c 5 203.188.247.3
tracepath -n -m 15 203.188.247.3

# 3. Non-calling SIP OPTIONS probe (requires python3)
python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
msg = b'OPTIONS sip:ping@203.188.247.3:5060 SIP/2.0\r\nVia: SIP/2.0/UDP 0.0.0.0:5060;rport;branch=z9hG4bKprobe1\r\nFrom: <sip:probe@203.188.247.3>;tag=p1\r\nTo: <sip:ping@203.188.247.3>\r\nCall-ID: probe-1@local\r\nCSeq: 1 OPTIONS\r\nMax-Forwards: 70\r\nContent-Length: 0\r\n\r\n'
s.sendto(msg, ('203.188.247.3', 5060))
data, addr = s.recvfrom(4096)
print('Reply from', addr, '->', data.decode(errors='replace').splitlines()[0])
"
```

> If `ping` is `< 10 ms` and UDP 5060 replies immediately, your VPS has direct domestic / BDIX-grade routing to the IP-PBX.

---

## 2. Configure SIP Gateway in C-Call Admin Panel

1. Log in as Super Admin and open **SIP Gateways** (`/admin/providers`).
2. Click **+ Add SIP Provider** and enter:
   - **Provider Name**: e.g., `Gateway 09602111380`
   - **SIP Domain / Host**: `203.188.247.3`
   - **SIP Port**: `5060`
   - **SIP Username**: `09602111380`
   - **SIP Password**: `<YOUR_TRUNK_PASSWORD>` *(stored Fernet-encrypted using `SIP_PASSWORD_ENCRYPTION_KEY`)*
   - **Max Channels**: `10` (match your IP-PBX channel allocation)
   - **Status**: `Active`
3. Click **Save**, then click **Run SIP Diagnostic** on the gateway row.
4. Confirm the diagnostic panel shows **`PASS — REGISTER 200 OK`**. *(Note: `OPTIONS 404` is normal on Asterisk trunks when no extension is targeted.)*
5. Go to **SIP CLI Pool** (`/admin/numbers`), add your outbound CLI number (e.g., `09602111380`), link it to the gateway, and assign it to a merchant tenant.

---

## 3. SIP & RTP Networking in Docker

In `docker-compose.staging.lowmem.yml`, `call_worker` is configured with:

```env
SIP_TRANSPORT=udp
SIP_BIND_IP=0.0.0.0
RTP_BIND_IP=0.0.0.0
SIP_ADVERTISED_IP=<YOUR_SERVER_PUBLIC_IP>
RTP_ADVERTISED_IP=<YOUR_SERVER_PUBLIC_IP>
SIP_PORT=5060
RTP_PORT_MIN=10000
RTP_PORT_MAX=20000
```

- **Signaling (`SIP_PORT`):** Outbound SIP uses `;rport` in the `Via` header so Asterisk routes replies back over NAT.
- **Two-Way Media (`RTP_PORT_MIN`–`RTP_PORT_MAX`):** If your IP-PBX does not use symmetric RTP (`comedia`), or if inbound audio is silent during a live test call, publish a narrow RTP range (e.g. `10000-10050/udp` for low concurrency) on `call_worker` **only after** applying the carrier-IP firewall restrictions below.

---

## 4. Carrier-Restricted SIP & RTP Firewall Rules (Security Mandatory)

**NEVER expose UDP 5060 or RTP ports `10000–20000` to `0.0.0.0/0`.** Public SIP ports are scanned and toll-fraud targeted within minutes.

### Option A: UFW (Recommended on a Dedicated New VPS)

Allow only SSH, HTTP/HTTPS, Coolify (if local), and SIP/RTP **strictly from your IP-PBX carrier IP** (`203.188.247.3/32`):

```bash
ufw default deny incoming
ufw default allow outgoing

# SSH & Web/TLS
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp

# Restrict SIP & RTP strictly to your authorized IP-PBX carrier IP
ufw allow from 203.188.247.3/32 to any port 5060 proto udp
ufw allow from 203.188.247.3/32 to any port 10000:20000 proto udp

ufw enable
ufw status verbose
```

### Option B: Docker `DOCKER-USER` Chain (When Publishing Container Ports on a Shared Host)

Docker bypasses standard `INPUT` rules for published container ports. To restrict published SIP/RTP ports to `203.188.247.3/32` without disturbing existing Coolify apps:

```bash
# Allow SIP/RTP only from carrier IP 203.188.247.3
iptables -I DOCKER-USER -p udp --dport 5060 -s 203.188.247.3/32 -j RETURN
iptables -I DOCKER-USER -p udp --dport 5060 ! -s 203.188.247.3/32 -j DROP

iptables -I DOCKER-USER -p udp --dport 10000:20000 -s 203.188.247.3/32 -j RETURN
iptables -I DOCKER-USER -p udp --dport 10000:20000 ! -s 203.188.247.3/32 -j DROP
```

> Always test SSH access in a second terminal before closing your current session when applying firewall rules.
