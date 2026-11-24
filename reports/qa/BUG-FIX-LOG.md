# C-Call — Bug Fix Log

| ID | Issue | Root Cause | Fix | Verification |
|---|---|---|---|---|
| BUG-01 | Silent fallback Fernet key in production | `sip_provider._get_fernet()` used `FALLBACK_KEY` even when `FLASK_ENV=production` | Raised `RuntimeError` in prod if `SIP_PASSWORD_ENCRYPTION_KEY` missing/invalid | `test_sip_provider_fernet_fails_closed_in_production` (**PASS**) |
| BUG-02 | Cross-tenant `flow_id` IDOR in `quick_call` | `flow_id` parsed from form without `tenant_id` check | Added `SELECT id FROM flows WHERE id = %s AND tenant_id = %s` + `escape(str(e))` | `test_quick_call_blocks_cross_tenant_flow_idor` (**PASS**) |
| BUG-03 | Cross-tenant leak in `/api/debug/live-registry` | Called `list_active_calls(tenant_id=None)` for any logged-in user | Restricted to `DEBUG` + `super_admin` (404 otherwise) | `test_debug_live_registry_blocked_for_non_super_admin` + Playwright (**PASS**) |
| BUG-04 | Path traversal in `/recordings/play` & unauthenticated `/sounds/<f>` | Missing `realpath` prefix check & missing `@require_login` | Enforced `os.path.realpath` containment + `@require_login` | `test_play_recording_blocks_path_traversal`, `test_serve_sound_requires_authentication` (**PASS**) |
| BUG-05 | IVR retry prompt swallows barge-in DTMF & wrong `reached_terminal` | Return value of `_play_prompt()` discarded on retry; `reached_terminal=True` set after loop | Captured `barge = self._play_prompt(...)`; set `reached_terminal=False` on missing root/target/cycle | `TestV2CallingReliabilityAndIVR` (**PASS**) |
| BUG-06 | `GET /auth/login` triggered 429 rate limit during redirects | `@limiter.limit("10/minute")` applied to `GET` as well as `POST` | Scoped `@limiter.limit(..., methods=["POST"])` | Playwright full admin + merchant walk (**PASS**) |
| BUG-07 | COD DTMF `3` (human assistance) mapped to `FAILED` | `update_status_from_call` only handled `1`, `2`, and `0` | Mapped `dtmf_digit in ("3", "0")` to `human_agent_requested=True` & `order.agent_requested` | `test_human_agent_dtmf0_webhook_event` (**PASS**) |
