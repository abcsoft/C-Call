# C-Call — Gap Analysis

1. **Completed & Closed Gaps**:
   - Admin & Merchant RBAC, CSRF, fail-closed Fernet key enforcement in production (`app/config.py`, `app/models/sip_provider.py`).
   - Recording & sound file path-traversal confinement and authentication (`app/blueprints/customer/routes.py`).
   - API key scopes (`orders:read`, `orders:write`), per-key daily quotas, soft revocation, and rotation lineage (`migrations/028_v2_security_and_reliability.sql`).
   - Outbound compliance: destination prefix allowlist, opt-out/DND blocklist, daily spend limit, consent tracking (`app/services/compliance_service.py`).
   - IVR state machine: retry prompt barge-in capture, missing node/root/cycle error handling, and `/flows/<id>/validate` (`call_engine/ivr_engine.py`, `app/blueprints/customer/flows.py`).
   - COD workflow: DTMF `1` (confirm), `2` (cancel), `3`/`0` (human assistance -> `order.agent_requested`), plus [UCART_INTEGRATION.md](file:///c:/xampp_lite_8_3/www/ccall/docs/UCART_INTEGRATION.md).
   - Login/register rate-limiter `methods=["POST"]` scoping so `GET` redirects do not trigger 429.

2. **Remaining Non-Blocking / Environment Gaps**:
   - **HTMX syntax warning**: Browser console recorded 3 `htmx:syntax:error` messages on merchant pages; did not block any tested page or form submission (Low severity).
   - **Live Docker & Live SIP Trunk**: Docker Engine is not installed on the Windows QA host, and live carrier SIP calls were not placed without explicit authorization.
   - **Pre-existing uncommitted work on `main`**: Left untouched (`tts_concurrent.py`, `test_plan_lifecycle.py`, etc.) to preserve existing user work.
