# C-Call IVR WooCommerce Plugin Setup Guide

This guide describes how to install, configure, and test the **C-Call IVR Order Confirmation** plugin for WooCommerce to automate Cash on Delivery (COD) order verifications.

---

## 1. Prerequisites

Before installing the plugin, ensure you have:
* An active merchant account on **C-Call IVR** (`https://app.ccall.com.bd`).
* A WordPress website running **WooCommerce 5.8** or higher.
* SSL enabled on your WordPress website (`https://` is required for webhooks).

---

## 2. Installation

1. Download the `ccall-ivr` plugin folder.
2. Upload the folder to your WordPress plugins directory: `/wp-content/plugins/`.
3. Log in to your WordPress dashboard, navigate to **Plugins > Installed Plugins**, and click **Activate** under **C-Call IVR Order Confirmation**.

---

## 3. Retrieving API Credentials

To connect WooCommerce to C-Call, you need to retrieve your credentials from the C-Call Dashboard:

1. Log in to `https://app.ccall.com.bd`.
2. Go to **Settings > Integrations**:
   - **X-API-Key**: Click **Generate New Key** (copy the full key, as it will only be shown once).
   - **Webhook Secret**: Locate your webhook signing secret (used to verify incoming confirmation alerts).
   - **Base API URL**: Use `https://app.ccall.com.bd/api/v1` (or your custom server route).

---

## 4. Configuring WooCommerce

1. In your WordPress dashboard, navigate to **WooCommerce > C-Call IVR**.
2. Fill in the configuration fields:
   - **Enable Integration**: Check the box to activate calls.
   - **C-Call Base API URL**: Paste the API URL.
   - **X-API-Key Credentials**: Paste your generated API key.
   - **Trigger Order Status**: Choose when the call should fire. For COD, **Pending payment** or **On hold** is recommended.
   - **C-Call Webhook Secret**: Paste the webhook secret.
3. Click **Save Configuration**.

---

## 5. Setting Up the Webhook in C-Call

To let C-Call send verification updates back to WooCommerce:

1. On the WooCommerce settings page under **WooCommerce > C-Call IVR**, copy the **Your Store Webhook URL** (e.g., `https://yourdomain.com/wp-json/ccall/v1/webhook`).
2. Log in to your C-Call merchant dashboard.
3. Go to **Settings > Webhook Configuration** and paste this URL into the **Webhook URL** field.
4. Click **Save Webhook Settings**.

---

## 6. Testing & Diagnostics

The plugin includes a **System Test Console** tab to verify integration health before launching live:

### 1. Route Diagnostics
- Navigate to the **System Test Console** tab.
- Click **Test Connection**.
- The system will run a connection check to verify route availability. If successful, you will see a green checkmark indicating the parameters are verified.

### 2. Test Call Dispatch
- In the **Send Test Confirmation Call** card, type a recipient phone number (standard Bangladesh number format, e.g. `01712345678`).
- Click **Send Test Call**.
- This will queue a mock order and trigger a test call. Verify that your phone rings and that call results are logged.

---

## 7. Security & Verification

To prevent PII leakage and spoofing:
* Outgoing order details are automatically normalized to remove non-numeric values and format international prefixes.
* All admin AJAX requests verify **WP Nonces** and enforce **manage_options** administrator-level capabilities checks.
* Incoming webhook payload signatures are verified using HMAC-SHA256. The payload spacing matches Python's dictionary representation (`{key: value, ...}`) using a custom PHP serializer (`python_json_dumps`).
* Order status changes are logged securely inside WooCommerce private order notes, keeping a full audit trail.
