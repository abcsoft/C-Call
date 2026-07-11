<?php
/**
 * CCAll_Admin_Actions — Adds "Resend C-Call Verification" action and order detail metabox.
 *
 * @package CCallIvr
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class CCAll_Admin_Actions {

	/**
	 * Register hooks.
	 */
	public static function init() {
		// Order actions dropdown.
		add_filter( 'woocommerce_order_actions', array( __CLASS__, 'add_resend_action' ) );
		add_action( 'woocommerce_order_action_ccall_resend', array( __CLASS__, 'handle_resend_action' ) );

		// Order detail metabox.
		add_action( 'add_meta_boxes', array( __CLASS__, 'add_metabox' ) );

		// AJAX handler for metabox resend button.
		add_action( 'wp_ajax_ccall_resend_call', array( __CLASS__, 'ajax_resend_call' ) );
	}

	/**
	 * Add "Resend C-Call Verification" to the order actions dropdown.
	 *
	 * @param array $actions Existing actions.
	 * @return array Modified actions.
	 */
	public static function add_resend_action( $actions ) {
		$actions['ccall_resend'] = __( 'Resend C-Call Verification', 'ccall-ivr' );
		return $actions;
	}

	/**
	 * Handle the resend action from the order actions dropdown.
	 *
	 * @param WC_Order $order Order object.
	 */
	public static function handle_resend_action( $order ) {
		$order_id = $order->get_id();

		CCAll_Logger::info( 'Manual resend triggered (order action)', array( 'order' => $order_id ) );

		$result = CCAll_API::send_order( $order_id, true );

		if ( $result ) {
			$order->add_order_note( __( 'C-Call IVR: Manual resend dispatched successfully.', 'ccall-ivr' ) );
		} else {
			$order->add_order_note( __( 'C-Call IVR: Manual resend failed. Check Diagnostic Logs.', 'ccall-ivr' ) );
		}
	}

	/**
	 * Register the C-Call status metabox on the order edit page.
	 */
	public static function add_metabox() {
		$screen = wc_get_container()->get( \Automattic\WooCommerce\Internal\DataStores\Orders\CustomOrdersTableController::class )->custom_orders_table_usage_is_enabled()
			? wc_get_page_screen_id( 'shop-order' )
			: 'shop_order';

		add_meta_box(
			'ccall-ivr-status',
			__( 'C-Call IVR Status', 'ccall-ivr' ),
			array( __CLASS__, 'render_metabox' ),
			$screen,
			'side',
			'default'
		);
	}

	/**
	 * Render the C-Call IVR Status metabox.
	 *
	 * @param WP_Post|WC_Order $post_or_order Post object or order object.
	 */
	public static function render_metabox( $post_or_order ) {
		$order = ( $post_or_order instanceof WC_Order ) ? $post_or_order : wc_get_order( $post_or_order->ID );
		if ( ! $order ) {
			echo '<p>' . esc_html__( 'Order not found.', 'ccall-ivr' ) . '</p>';
			return;
		}

		$status   = $order->get_meta( '_ccall_status' ) ?: '—';
		$call_id  = $order->get_meta( '_ccall_call_id' ) ?: '—';
		$dtmf     = $order->get_meta( '_ccall_dtmf_digit' ) ?: '—';
		$attempts = $order->get_meta( '_ccall_attempt_count' ) ?: '0';
		$error    = $order->get_meta( '_ccall_last_error' ) ?: '—';
		$last_wh  = $order->get_meta( '_ccall_last_webhook_at' ) ?: '—';

		$nonce = wp_create_nonce( 'ccall_resend_nonce' );
		?>
		<style>
			.ccall-meta-table { width: 100%; border-collapse: collapse; }
			.ccall-meta-table td { padding: 5px 4px; font-size: 12px; border-bottom: 1px solid #f0f0f0; }
			.ccall-meta-table td:first-child { font-weight: 600; color: #555; width: 45%; }
			.ccall-meta-table td:last-child { color: #1d2327; }
			.ccall-resend-btn { margin-top: 10px; }
			.ccall-status-value { text-transform: uppercase; font-weight: 700; }
			.ccall-status-confirmed { color: #065f46; }
			.ccall-status-cancelled, .ccall-status-failed { color: #991b1b; }
			.ccall-status-queued { color: #1d4ed8; }
			.ccall-status-no_answer { color: #9a3412; }
		</style>
		<table class="ccall-meta-table">
			<tr><td><?php esc_html_e( 'Status', 'ccall-ivr' ); ?></td><td><span class="ccall-status-value ccall-status-<?php echo esc_attr( $status ); ?>"><?php echo esc_html( $status ); ?></span></td></tr>
			<tr><td><?php esc_html_e( 'Call ID', 'ccall-ivr' ); ?></td><td><?php echo esc_html( $call_id ); ?></td></tr>
			<tr><td><?php esc_html_e( 'DTMF Digit', 'ccall-ivr' ); ?></td><td><?php echo esc_html( $dtmf ); ?></td></tr>
			<tr><td><?php esc_html_e( 'Attempts', 'ccall-ivr' ); ?></td><td><?php echo esc_html( $attempts ); ?></td></tr>
			<tr><td><?php esc_html_e( 'Last Error', 'ccall-ivr' ); ?></td><td><?php echo esc_html( $error ); ?></td></tr>
			<tr><td><?php esc_html_e( 'Last Webhook', 'ccall-ivr' ); ?></td><td><?php echo esc_html( $last_wh ); ?></td></tr>
		</table>
		<button type="button" class="button ccall-resend-btn" id="ccall-resend-btn" data-order-id="<?php echo esc_attr( $order->get_id() ); ?>" data-nonce="<?php echo esc_attr( $nonce ); ?>">
			<?php esc_html_e( 'Resend C-Call Verification', 'ccall-ivr' ); ?>
		</button>
		<span id="ccall-resend-result" style="display:none; margin-left:8px; font-size:12px;"></span>
		<script>
		jQuery(function($){
			$('#ccall-resend-btn').on('click', function(){
				var btn = $(this);
				btn.prop('disabled', true).text('<?php echo esc_js( __( 'Sending...', 'ccall-ivr' ) ); ?>');
				$('#ccall-resend-result').hide();
				$.post(ajaxurl, {
					action: 'ccall_resend_call',
					order_id: btn.data('order-id'),
					nonce: btn.data('nonce')
				}, function(res){
					btn.prop('disabled', false).text('<?php echo esc_js( __( 'Resend C-Call Verification', 'ccall-ivr' ) ); ?>');
					var r = $('#ccall-resend-result');
					if(res.success){
						r.css('color','#065f46').text('✓ ' + res.data.message).show();
					} else {
						r.css('color','#991b1b').text('✗ ' + (res.data ? res.data.message : 'Failed')).show();
					}
				});
			});
		});
		</script>
		<?php
	}

	/**
	 * AJAX handler for metabox resend button.
	 */
	public static function ajax_resend_call() {
		check_ajax_referer( 'ccall_resend_nonce', 'nonce' );

		if ( ! current_user_can( 'edit_shop_orders' ) ) {
			wp_send_json_error( array( 'message' => __( 'Insufficient permissions.', 'ccall-ivr' ) ), 403 );
		}

		$order_id = isset( $_POST['order_id'] ) ? absint( $_POST['order_id'] ) : 0;
		if ( ! $order_id ) {
			wp_send_json_error( array( 'message' => __( 'Invalid order ID.', 'ccall-ivr' ) ) );
		}

		CCAll_Logger::info( 'Manual resend triggered (metabox)', array( 'order' => $order_id ) );

		$result = CCAll_API::send_order( $order_id, true );

		if ( $result ) {
			wp_send_json_success( array( 'message' => __( 'Call re-queued successfully.', 'ccall-ivr' ) ) );
		} else {
			wp_send_json_error( array( 'message' => __( 'Resend failed. Check C-Call settings and logs.', 'ccall-ivr' ) ) );
		}
	}
}
