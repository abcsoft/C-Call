# C-Call — Browser Test Report

- **Tool:** Playwright Chromium (`headless=True`) against live `http://127.0.0.1:5000` (`ccall`) and isolated `http://127.0.0.1:5001` (`ccall_test`, `BrowserTestConfig`, CSRF enabled)
- **Raw outputs:** [browser_qa_results.json](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/browser_qa_results.json), [browser_crud_results.json](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/browser_crud_results.json)
- **Screenshots:** `reports/qa/screenshots/`

## Summary

| Check | Result |
|---|---|
| Invalid login blocked (`qa_admin@ccall.example.com`) | **PASS** |
| Admin Panel Page Walk (12/12 routes incl. `/admin/tts-reservations`) | **PASS** (all HTTP 200) |
| Admin CRUD (Create Tenant modal, Credit Wallet modal, Add SIP Provider form) | **PASS** |
| Merchant Panel Page Walk (12/12 routes) | **PASS** (all HTTP 200) |
| Merchant IVR CRUD (Create flow, add Menu+Hangup nodes, add DTMF `1` transition, `/flows/<id>/validate` -> `valid: true`, delete flow) | **PASS** |
| Merchant API Key + Order CRUD (Generate key in `/integration`, `POST /api/v1/orders` -> 200 `CALL_QUEUED`, order visible in `/orders`) | **PASS** |
| Cross-Tenant Isolation (`qa_merchant_b` blocked from Merchant A flow, orders, and `/api/debug/live-registry` -> 404) | **PASS** |
| Mobile Responsive (`390x844` on `/dashboard`, `/orders`, `/flows`, `/wallet`, `/settings/subscription`) | **PASS** (`horizontal_overflow: false`) |
| Console Errors / Failed Requests / HTTP 500s (`task-1385`) | `page_errors: 0`, `http_500s: 0`; 1 transient Windows socket error `net::ERR_NO_BUFFER_SPACE` on `tailwind.css` during rapid localhost loop |
| Admin Customers Edit / Archive / Delete Guard / Safe Delete / Bulk Synthetic Cleanup (`task-1385`) | **PASS** (verified live financial-history block, empty-tenant deletion, and synthetic bulk cleanup in Chromium) |
| Admin SIP Diagnostics 404 Fail vs Digest REGISTER 200 Pass + `max_channels` Edit (`task-1385`) | **PASS** (HTML partial card rendered with non-calling caveat, 0 secret exposure) |
| Admin Plans Duplicate & TTS Usage Cost + CSV Export (`task-1385`) | **PASS** (`-copy` created, `$0.0480` rendered, `/admin/tts-usage/export.csv` HTTP 200) |
| Merchant 12-Route Walk & Order API (`task-1385`) | **PASS** (12/12 HTTP 200; `POST /api/v1/orders` -> `200 CALL_QUEUED`; `0` JS `page_errors` after `revenue.html` & `retry.html` fixes) |
| CSP & HTMX Fixes (`settings.py`, `integration.html`, `flows.html`, `flow_edit.html`, `script.html`, `cdr.html`, `revenue.html`, `providers.html`, `plans/list.html`, `customers/list.html`, `customers/detail.html`, `customer/settings.html`, `customer/subscription.html`, `customer/team.html`, `customer/orders/list.html`, `customer/retry.html`) | **FIXED** (`0` inline `onclick=`/`onsubmit=`/`onchange=` attributes remaining in `app/templates/`) |
