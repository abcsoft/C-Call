# C-Call — Browser Test Report

- **Tool:** Playwright Chromium (`headless=True`) against live `http://127.0.0.1:5000` (`ccall`) and isolated `http://127.0.0.1:5001` (`ccall_test`, `BrowserTestConfig`, CSRF enabled)
- **Raw outputs:** [live\_5000\_acceptance\_results.json](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/live_5000_acceptance_results.json), [SYNTHETIC-ACCOUNT-AUDIT-101.md](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/SYNTHETIC-ACCOUNT-AUDIT-101.md), [browser_qa_results.json](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/browser_qa_results.json), [browser_crud_results.json](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/browser_crud_results.json)
- **Screenshots:** `reports/qa/screenshots/`

## Summary

| Check | Result |
|---|---|
| Invalid login blocked (`qa_admin@ccall.example.com`) | **PASS** |
| Admin Panel Page Walk (`:5000` `task-1715` 11/11 routes incl. `/admin/plans`, `/admin/platform-tts`, `/admin/tts-usage`) | **PASS** (all HTTP 200) |
| Merchant Panel Page Walk (`:5000` `task-1715` 12/12 routes) | **PASS** (all HTTP 200) |
| Admin Live SIP Diagnostic (`203.188.247.3:5060` on `:5000` `task-1715`) | **PASS** (`OPTIONS: 404` & `REGISTER: 200` separate badges; header `PASS — REGISTER 200 OK (OPTIONS 404)`; non-disruptive RFC 3261 binding-query caveat; 0 secrets exposed) |
| Admin Plan Create → Duplicate → Archive → Activate (`:5000` `task-1715`) | **PASS** (`qa-accept-edgeonly` + `-copy`) |
| Merchant Plan Selection + Plan TTS Provider Restriction + Subscription Cancel (`:5000` `task-1715`) | **PASS** (`POST /api/settings/tts-preview` with `tts_provider="gemini"` denied `403` `Provider 'gemini' is not available on your plan.`; `cancel_at_period_end=true` persisted; 0 paid credits used) |
| Admin TTS Usage Accounting & CSV Export (`:5000` `task-1715`) | **PASS** (`2500` chars / `$0.1250` DB-to-UI & CSV match) |
| Live `:5000` Console / Page / HTTP 500 / Failed Requests / Secret Leaks / Existing Tenants (`task-1715`) | **PASS** (`0` console errors, `0` page errors, `0` HTTP 500s, `0` failed requests, `0` leaked secrets, `101 -> 101` tenants intact) |
| Admin CRUD (`:5001`: Create Tenant modal, Credit Wallet modal, Add SIP Provider form, Customer Edit/Archive/Delete Guard/Safe Delete/Bulk Cleanup) | **PASS** |
| Merchant IVR CRUD (`:5001`: Create flow, add Menu+Hangup nodes, add DTMF `1` transition, `/flows/<id>/validate` -> `valid: true`, delete flow) | **PASS** |
| Merchant API Key + Order CRUD (`:5001`: Generate key in `/integration`, `POST /api/v1/orders` -> 200 `CALL_QUEUED`, order visible in `/orders`) | **PASS** |
| Cross-Tenant Isolation (`qa_merchant_b` blocked from Merchant A flow, orders, and `/api/debug/live-registry` -> 404) | **PASS** |
| Mobile Responsive (`390x844` on `/dashboard`, `/orders`, `/flows`, `/wallet`, `/settings/subscription`) | **PASS** (`horizontal_overflow: false`) |
| CSP & HTMX Fixes (`settings.py`, `integration.html`, `flows.html`, `flow_edit.html`, `script.html`, `cdr.html`, `revenue.html`, `providers.html`, `plans/list.html`, `customers/list.html`, `customers/detail.html`, `customer/settings.html`, `customer/subscription.html`, `customer/team.html`, `customer/orders/list.html`, `customer/retry.html`) | **FIXED** (`0` inline `onclick=`/`onsubmit=`/`onchange=` attributes remaining in `app/templates/`) |
