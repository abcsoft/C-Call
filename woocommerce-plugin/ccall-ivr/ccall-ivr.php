<?php
/**
 * Plugin Name: C-Call IVR Order Confirmation
 * Description: Automatically call customers for COD order confirmation via C-Call IVR.
 * Version: 1.0.0
 * Author: C-Call
 * License: GPL-2.0+
 * Text Domain: ccall-ivr
 *
 * @package CCallIvr
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit; // Exit if accessed directly.
}

// Define core directory constants
define( 'CCALL_IVR_VERSION', '1.0.0' );
define( 'CCALL_IVR_PATH', plugin_dir_path( __FILE__ ) );
define( 'CCALL_IVR_URL', plugin_dir_url( __FILE__ ) );

// Declare HPOS Compatibility
add_action( 'before_woocommerce_init', function() {
	if ( class_exists( \Automattic\WooCommerce\Utilities\FeaturesUtil::class ) ) {
		\Automattic\WooCommerce\Utilities\FeaturesUtil::declare_compatibility(
			'custom_order_tables', __FILE__, true
		);
	}
});

// Require modular includes
require_once CCALL_IVR_PATH . 'includes/class-ccall-logger.php';
require_once CCALL_IVR_PATH . 'includes/class-ccall-api.php';
require_once CCALL_IVR_PATH . 'includes/class-ccall-webhook.php';
require_once CCALL_IVR_PATH . 'includes/class-ccall-admin-columns.php';
require_once CCALL_IVR_PATH . 'includes/class-ccall-admin-actions.php';
require_once CCALL_IVR_PATH . 'includes/class-ccall-settings.php';

// Check if WooCommerce is active before instantiating the plugin.
add_action( 'plugins_loaded', 'ccall_ivr_init_plugin', 11 );
function ccall_ivr_init_plugin() {
	if ( class_exists( 'WooCommerce' ) ) {
		CCAll_IVR_Plugin::get_instance();
	} else {
		add_action( 'admin_notices', 'ccall_ivr_woocommerce_inactive_notice' );
	}
}

function ccall_ivr_woocommerce_inactive_notice() {
	if ( current_user_can( 'activate_plugins' ) ) {
		?>
		<div class="error notice is-dismissible">
			<p><strong><?php esc_html_e( 'C-Call IVR Order Confirmation', 'ccall-ivr' ); ?></strong> <?php esc_html_e( 'requires WooCommerce to be installed and active. Please install or activate WooCommerce to use this plugin.', 'ccall-ivr' ); ?></p>
		</div>
		<?php
	}
}

class CCAll_IVR_Plugin {

	/**
	 * Singleton Instance.
	 *
	 * @var CCAll_IVR_Plugin|null
	 */
	private static $instance = null;

	/**
	 * Returns singleton instance of the class.
	 *
	 * @return CCAll_IVR_Plugin
	 */
	public static function get_instance() {
		if ( null === self::$instance ) {
			self::$instance = new self();
		}
		return self::$instance;
	}

	/**
	 * Hook registrations.
	 */
	private function __construct() {
		// Core hooks for calling triggers
		add_action( 'woocommerce_checkout_order_processed', array( $this, 'trigger_call_on_checkout' ), 10, 3 );
		add_action( 'woocommerce_order_status_changed', array( $this, 'trigger_call_on_status_change' ), 10, 3 );

		// Async handler: runs C-Call API outside the checkout request so the
		// customer's page load is not blocked waiting for C-Call's 15s timeout.
		add_action( 'ccall_send_order_async', array( 'CCAll_API', 'send_order' ) );

		// Webhook REST API registration
		add_action( 'rest_api_init', array( 'CCAll_Webhook', 'register_route' ) );

		// Initialize modular classes
		CCAll_Settings::init();
		CCAll_Admin_Columns::init();
		CCAll_Admin_Actions::init();
	}

	/**
	 * Check if the order meets general call trigger requirements.
	 *
	 * @param \WC_Order $order The WooCommerce Order.
	 * @return bool
	 */
	private function should_trigger_call( $order ) {
		if ( ! get_option( 'ccall_enabled' ) ) {
			return false;
		}

		// COD only guard
		if ( get_option( 'ccall_cod_only' ) ) {
			if ( $order->get_payment_method() !== 'cod' ) {
				return false;
			}
		}

		// Allowed payment methods guard
		$allowed_methods = get_option( 'ccall_allowed_payment_methods', array() );
		if ( ! empty( $allowed_methods ) && is_array( $allowed_methods ) ) {
			if ( ! in_array( $order->get_payment_method(), $allowed_methods, true ) ) {
				return false;
			}
		}

		return true;
	}

	/**
	 * Triggers C-Call order confirmation on successful checkout.
	 *
	 * @param int            $order_id Order ID.
	 * @param array          $posted_data Form posted data.
	 * @param \WC_Order|null $order Order Object.
	 */
	public function trigger_call_on_checkout( $order_id, $posted_data = array(), $order = null ) {
		if ( ! $order ) {
			$order = wc_get_order( $order_id );
		}

		if ( ! $order ) {
			return;
		}

		if ( ! $this->should_trigger_call( $order ) ) {
			return;
		}

		$trigger_status = get_option( 'ccall_trigger_status', 'pending' );
		$current_status = $order->get_status();

		if ( $current_status === $trigger_status ) {
			// Schedule async so the C-Call API call doesn't block checkout page load.
			wp_schedule_single_event( time(), 'ccall_send_order_async', array( $order_id ) );
		}
	}

	/**
	 * Triggers C-Call order confirmation when order status changes to the configured trigger status.
	 *
	 * @param int    $order_id Order ID.
	 * @param string $old_status Old status.
	 * @param string $new_status New status.
	 */
	public function trigger_call_on_status_change( $order_id, $old_status = '', $new_status = '' ) {
		$order = wc_get_order( $order_id );
		if ( ! $order ) {
			return;
		}

		if ( ! $this->should_trigger_call( $order ) ) {
			return;
		}

		$trigger_status = get_option( 'ccall_trigger_status', 'pending' );

		if ( empty( $new_status ) ) {
			$new_status = $order->get_status();
		}

		if ( $new_status === $trigger_status ) {
			wp_schedule_single_event( time(), 'ccall_send_order_async', array( $order_id ) );
		}
	}
}
