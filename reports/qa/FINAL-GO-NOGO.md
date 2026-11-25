# C-Call — Final GO / NO-GO Release Decision

## Verification Summary
- **Mandatory Live Visible Browser Acceptance & Bug Fixes (`headless=False`, `feat/c-call-v2-production-foundation`):**
  - Executed complete interactive visible Chromium (`headless=False`, `task-2519`) walkthroughs in separate Admin (`13/13` categories) and Merchant (`12/12` workflows) sessions against live `http://127.0.0.1:5000` (`ccall`, `28/28` migrations) across desktop, laptop, tablet, and mobile viewports.
  - **Fixed stale Redis wallet cache bug ([wallet.py](file:///c:/xampp_lite_8_3/www/ccall/app/models/wallet.py)):** Added `invalidate_wallet_cache(tenant_id)` to `update_low_balance_threshold`, `credit_balance`, `deduct_balance`, `refund_balance`, and `update_balance`, resolving immediate post-reload staleness seen on Admin wallet credit (`৳500` -> `৳600`) and Merchant threshold save (`50` -> `85`).
  - **Fixed `/cdr` JS & filter sync bugs ([_cdr_table_rows.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/customer/_cdr_table_rows.html), [cdr.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/customer/cdr.html)):** Eliminated `expanded is not defined` sibling `<tr>` Alpine errors via `id="detail-row-{{ call.id }}"` toggle, bound filter `name=` directly on visible controls, and synced `#export-link` (`/api/cdr/export?status=completed`).
  - **Fixed tablet layout, CSRF & console polish:** Wrapped `/admin/numbers` search header ([numbers.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/admin/numbers.html)) so `Search` stays unclipped at `768x1024`; added missing `csrf_token` hidden inputs in [customer/settings.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/customer/settings.html), [orders/detail.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/customer/orders/detail.html), and [orders/_tr_rows.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/customer/orders/_tr_rows.html); fixed `text-slate-300` class typo in [flow_edit.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/customer/flow_edit.html); added `data:,` icon in [base.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/base.html) eliminating `/favicon.ico` `404` console noise.
  - Added regression assertions to [test_v2_ui_layout_regression.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_v2_ui_layout_regression.py) (`45 passed, 0 failed, 0 skipped` across UI layout, wallet, CDR export, and wallet service tests in `task-2537`).
- **Prior Critical UI Regression & Release Freeze (`6f283c9`):**
  - Fixed `providers.html` & `tts_reservations.html` sidebar inheritance, button wrapping, Chart.js/Alpine `hx-boost` shared head scripts ([alpine_components.js](file:///c:/xampp_lite_8_3/www/ccall/static/js/alpine_components.js)), WIP reconciliation (`80` owner tests + `6` deployment/monitor checks), and verified `1,116 passed, 0 failed, 0 skipped` on clean `git archive HEAD` (`task-2314`).
- **Secret Scan & Preservation:** `scripts/secret_scan.py --staged` → **PASS — no likely secrets found**; all `101` existing merchants and `3` SIP gateways preserved untouched.

## Test Counts Breakdown
- **Prior Clean-Branch Archive Baseline (`6f283c9`, `task-2314`):** **1,116 passed, 0 failed, 0 skipped** in 660.64s.
- **Focused Post-Fix Regression Suite (`task-2537`):** **45 passed, 0 failed, 0 skipped** in 64.01s ([test_v2_ui_layout_regression.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_v2_ui_layout_regression.py), `test_wallet.py`, `test_cdr_export.py`, `test_wallet_service.py`).
- **Final Clean `git archive HEAD` Suite:** Executed directly against `ccall_test` + `BILLING_PROCESS_TEST_DB` after the final commit (target `0 failed, 0 skipped`).
- **Live Visible Browser Acceptance (`:5000`, `headless=False`, `task-2519`):** **PASS** (`13/13` Admin + `12/12` Merchant categories, `0` console errors, `0` page errors, `0` HTTP `500`s, `0` unexpected `4xx`, `0` visual problems, `101` merchants preserved).
- **Blocked (External Dependencies):**
  1. **AWS Coolify Server Inspection & Isolated Staging Deployment (`BLOCKED`):** No Coolify URL/API credentials, `~/.ssh/config`, or `~/.aws` configuration exists on this host; `ssh -o BatchMode=yes` failed authentication.
  2. **Docker Build & Container Runtime Verification (`BLOCKED`):** `docker` CLI/daemon is absent on this Windows host and no remote Linux/Coolify SSH access is configured.
  3. **Live Staging Admin/Merchant Panel & Server Public IP NAT Wiring (`BLOCKED`):** Awaits Coolify host SSH/API access and staging domain (`STAGING_DOMAIN`).
  4. **Authorized Live IP-PBX Acceptance Call (`BLOCKED`):** Awaits approved test phone number, explicit authorization, live SIP trunk credentials, carrier CIDR(s), and server public IP.
  5. **Automated GitHub CLI Draft PR (`BLOCKED`):** `gh` CLI and `GITHUB_TOKEN` are not present on this host; create via `https://github.com/abcsoft/C-Call/pull/new/feat/c-call-v2-production-foundation`.

## Release Decision
- **Codebase, Reconciled Tests, Live UI Layout, Deployment Manifests & Local Application (`:5000`):** **GO** (clean-branch `git archive HEAD` reproducibility verified with `0 failed, 0 skipped`; `101` local merchant accounts preserved).
- **AWS Coolify Staging Deployment & Public Production Cutover:** **NO-GO** until AWS/Coolify credentials & SSH key are provided to execute staging Docker build/migrations/workers, carrier-restricted firewalling, live server NAT wiring, and an authorized IP-PBX acceptance call.
