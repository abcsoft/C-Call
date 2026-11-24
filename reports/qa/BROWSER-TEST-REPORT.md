# C-Call — Browser Test Report

- **Tool:** Playwright Chromium (`headless=True`) against live `http://127.0.0.1:5000`
- **Raw output:** [browser_qa_results.json](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/browser_qa_results.json)
- **Screenshots:** `reports/qa/screenshots/` (`admin_admin_dashboard.png`, `admin_admin_customers.png`, `admin_admin_providers.png`, `merchant_dashboard.png`, `merchant_flows.png`, `merchant_integration.png`, `ivr_flow_edit.png`, `merchant_mobile_dashboard.png`)

## Summary

| Check | Result |
|---|---|
| Invalid login blocked (`qa_admin@ccall.example.com`) | **PASS** |
| Admin Panel (11/11 routes) | **PASS** (all HTTP 200) |
| Merchant Panel (12/12 routes) | **PASS** (all HTTP 200) |
| IVR Flow Creation (`/flows/new`) & `/flows/7/validate` | **PASS** (HTTP 400 JSON for empty flow without root) |
| Cross-Tenant Isolation (`qa_merchant_b` -> Merchant A flow & `/api/debug/live-registry`) | **PASS** (blocked / 404) |
| Mobile Responsive (`390x844` on `/dashboard`, `/orders`, `/flows`) | **PASS** (`horizontal_overflow: false`) |
| HTTP 500s / Failed Network Requests | **0** |
| Console Errors | 3 `htmx:syntax:error` (Low severity, non-blocking) + 2 intentional negative-test 400/404 |
