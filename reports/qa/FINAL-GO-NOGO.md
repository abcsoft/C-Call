# C-Call — Final GO / NO-GO Release Decision

## Verification Summary
- **Complete Repository Pytest Suite (`pytest -q --tb=short`):** **1,051 passed, 18 skipped, 1 failed** (`test_bridge_leg_uses_advertised_ip_and_releases_port_on_failure` due to leaked ephemeral ports from prior `IvrCall` instantiations in earlier unit tests — fixed by adding `IvrCall.close()` / `__del__()` and scoping the range assertion, then verified `8 passed` in `test_v2_sip_networking.py`).
- **Live Browser QA (Playwright):**
  - Full page walk on `ccall` (`:5000`) and isolated `ccall_test` (`:5001`, `BrowserTestConfig` with CSRF enabled): 11/11 Admin routes + 12/12 Merchant routes HTTP 200, mobile layout (`390x844`) verified, cross-tenant isolation verified.
  - Full CRUD QA (`task-657`): Admin Create Tenant modal, Credit Wallet modal, Add SIP Provider, Merchant IVR Flow/Node/Transition + `/validate` (`valid: true`), API Key generation + `POST /api/v1/orders` + UI order visibility, and CSP inline-handler fixes.
- **SIP/RTP & Multi-Process Networking:** 8/8 tests in `tests/unit/test_v2_sip_networking.py` + 24/24 in `tests/unit/test_direct_sip_ack.py` **PASS** (advertised vs bind IPs, `SIP_TRANSPORT` validation, multi-process RTP port exclusivity with `SO_EXCLUSIVEADDRUSE`, RTP source IP check, `CANCEL` while ringing, bridge-leg cleanup).
- **Secret Scan:** `scripts/secret_scan.py` across all 28 branch files → **PASS — no likely secrets found**.

## Remaining Blockers
1. **Docker Build & Container Startup (`BLOCKED`)**: Docker Engine/CLI is not installed on the Windows QA host.
2. **AWS Coolify Live Staging Provisioning (`BLOCKED`)**: No Coolify API credentials or SSH host target configured locally; isolated staging overlay [docker-compose.staging.coolify.yml](file:///c:/xampp_lite_8_3/www/ccall/docker-compose.staging.coolify.yml) is prepared.
3. **Authorized Live IP-PBX Acceptance Call (`BLOCKED`)**: No live carrier credentials or authorized test number configured; non-dialing diagnostic and gated acceptance harness [ippbx_acceptance.py](file:///c:/xampp_lite_8_3/www/ccall/scripts/ippbx_acceptance.py) is ready.
4. **GitHub CLI Draft PR (`BLOCKED` from CLI)**: `gh` CLI is not installed on this host; branch `feat/c-call-v2-production-foundation` is pushed to `origin` with the PR creation link ready.

## Release Decision
- **Isolated Coolify / Docker Staging Deployment:** **GO** (compose, migrations `001`–`028`, WSGI, Redis, workers, NAT/RTP env vars, and CyberPanel/Coolify docs are ready).
- **Public Production Cutover:** **NO-GO** until Docker build/container startup on the Linux host and an authorized live IP-PBX acceptance call (`scripts/ippbx_acceptance.py --mode full-call --confirm-authorized-test --target <number>`) pass.
