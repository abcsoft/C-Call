=== C-Call IVR Order Confirmation ===
Contributors: ccall
Tags: woocommerce, ivr, call, confirmation, cod, bangladesh, verification
Requires at least: 5.8
Tested up to: 6.5
Requires PHP: 7.4
Stable tag: 1.0.0
License: GPLv2 or later
License URI: http://www.gnu.org/licenses/gpl-2.0.html

Automatically call customers for Cash on Delivery (COD) order confirmation via C-Call IVR.

== Description ==

C-Call IVR Order Confirmation is a WooCommerce integration plugin that automatically initiates automated calls to customers in Bangladesh to verify Cash on Delivery (COD) orders.

When an order reaches your designated status (e.g., Pending or Processing), the plugin queues an interactive call via C-Call. The customer can confirm by pressing 1, which changes the order status in WooCommerce to Processing, or cancel by pressing 2, which changes the order status to Cancelled.

= Features =
* Automatic IVR confirmation call triggers on checkout or order status change.
* Zero-downtime, fully asynchronous call handling using C-Call APIs.
* Secure webhook signature checking using HMAC-SHA256.
* Full call outcome logging direct to WooCommerce order notes.
* Diagnostic test console for connection testing and trial calls.
* Clean and normalized phone number processing for Bangladeshi mobile operators.

== Installation ==

1. Upload the entire `ccall-ivr` directory to the `/wp-content/plugins/` directory of your WordPress site.
2. Activate the plugin through the 'Plugins' menu in WordPress.
3. Navigate to WooCommerce > C-Call IVR in your admin panel.
4. Input your Base API URL, X-API-Key, and Webhook Secret from the C-Call merchant dashboard.
5. Save the configuration and verify the connection with the "Test Connection" button.
6. Copy the provided Webhook URL and paste it into your C-Call dashboard.

== Changelog ==

= 1.0.0 =
* Initial release of the C-Call IVR WooCommerce Integration Plugin.
