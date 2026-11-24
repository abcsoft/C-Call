# C-Call — Security Audit Summary

Full line-by-line findings are documented in [V2_DEEP_AUDIT_REPORT.md](file:///c:/xampp_lite_8_3/www/ccall/docs/V2_DEEP_AUDIT_REPORT.md). Key verified controls:

| Control | Finding / Verification | Status |
|---|---|---|
| Production `SECRET_KEY` & `SIP_PASSWORD_ENCRYPTION_KEY` | Enforced non-placeholder & valid Fernet key at boot (`app/config.py`) | **FIXED / PASS** |
| Recording & Sound Path Traversal | `os.path.realpath` containment + `@require_login` on `/sounds/<filename>` | **FIXED / PASS** |
| Cross-Tenant IDOR (`quick_call` & `/api/debug/live-registry`) | Verified `flows.tenant_id = g.tenant_id`; debug route returns 404 in non-debug/non-super_admin | **FIXED / PASS** |
| API Key Security | SHA-256 hashed, scoped (`orders:read`/`orders:write`), per-key `daily_quota`, soft-revoked with `audit_log` | **FIXED / PASS** |
| Outbound Compliance | Prefix allowlist, `opt_out_numbers`, `daily_spend_limit`, consent tracking | **FIXED / PASS** |
| Secret Scanning | `python scripts/secret_scan.py --staged` -> `PASS — no likely secrets found` | **PASS** |
