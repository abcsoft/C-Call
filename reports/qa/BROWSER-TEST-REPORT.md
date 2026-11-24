# C-Call — Browser Test Report

- **Tool:** Playwright Chromium (`headless=True`) against live `http://127.0.0.1:5000` (`ccall`) and isolated `http://127.0.0.1:5001` (`ccall_test`, `BrowserTestConfig`, CSRF enabled)
- **Raw outputs:** [browser_qa_results.json](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/browser_qa_results.json), [browser_crud_results.json](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/browser_crud_results.json)
- **Screenshots:** `reports/qa/screenshots/`

## Summary

| Check | Result |
|---|---|
| Invalid login blocked (`qa_admin@ccall.example.com`) | **PASS** |
| Admin Panel Page Walk (11/11 routes) | **PASS** (all HTTP 200) |
| Admin CRUD (Create Tenant modal, Credit Wallet modal, Add SIP Provider form) | **PASS** |
| Merchant Panel Page Walk (12/12 routes) | **PASS** (all HTTP 200) |
| Merchant IVR CRUD (Create flow, add Menu+Hangup nodes, add DTMF `1` transition, `/flows/<id>/validate` -> `valid: true`) | **PASS** |
| Merchant API Key + Order CRUD (Generate key in `/integration`, `POST /api/v1/orders` -> 200 `CALL_QUEUED`, order visible in `/orders`) | **PASS** |
| Cross-Tenant Isolation (`qa_merchant_b` blocked from Merchant A flow, orders, and `/api/debug/live-registry` -> 404) | **PASS** |
| Mobile Responsive (`390x844` on `/dashboard`, `/orders`, `/flows`) | **PASS** (`horizontal_overflow: false`) |
| HTTP 500s on final run | **0** |
| CSP Inline Handler Fixes (`settings.py`, `integration.html`) | **FIXED** (replaced `onclick=` with Alpine `@click`) |
