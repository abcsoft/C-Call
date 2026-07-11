<?php
/**
 * CCAll_Admin_Columns — Adds C-Call status badge column to WooCommerce orders list.
 *
 * Supports both legacy (CPT) and HPOS order storage.
 *
 * @package CCallIvr
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class CCAll_Admin_Columns {

	/**
	 * Register hooks for the orders list column.
	 */
	public static function init() {
		// Legacy CPT orders.
		add_filter( 'manage_edit-shop_order_columns', array( __CLASS__, 'add_column' ), 20 );
		add_action( 'manage_shop_order_posts_custom_column', array( __CLASS__, 'render_column' ), 10, 2 );

		// HPOS orders.
		add_filter( 'manage_woocommerce_page_wc-orders_columns', array( __CLASS__, 'add_column' ), 20 );
		add_action( 'manage_woocommerce_page_wc-orders_custom_column', array( __CLASS__, 'render_column_hpos' ), 10, 2 );

		// Inject badge styles.
		add_action( 'admin_head', array( __CLASS__, 'badge_styles' ) );
	}

	/**
	 * Add the C-Call column header.
	 *
	 * @param array $columns Existing columns.
	 * @return array Modified columns.
	 */
	public static function add_column( $columns ) {
		$new = array();
		foreach ( $columns as $key => $label ) {
			$new[ $key ] = $label;
			// Insert after "order_status" column.
			if ( 'order_status' === $key ) {
				$new['ccall_status'] = __( 'C-Call', 'ccall-ivr' );
			}
		}
		// Fallback: if order_status column not found, append.
		if ( ! isset( $new['ccall_status'] ) ) {
			$new['ccall_status'] = __( 'C-Call', 'ccall-ivr' );
		}
		return $new;
	}

	/**
	 * Render column content — legacy CPT mode.
	 *
	 * @param string $column    Column key.
	 * @param int    $post_id   Post ID (order ID).
	 */
	public static function render_column( $column, $post_id ) {
		if ( 'ccall_status' !== $column ) {
			return;
		}
		$order = wc_get_order( $post_id );
		if ( $order ) {
			echo self::get_badge_html( $order );
		}
	}

	/**
	 * Render column content — HPOS mode.
	 *
	 * @param string   $column Column key.
	 * @param WC_Order $order  Order object.
	 */
	public static function render_column_hpos( $column, $order ) {
		if ( 'ccall_status' !== $column ) {
			return;
		}
		echo self::get_badge_html( $order );
	}

	/**
	 * Generate badge HTML for an order.
	 *
	 * @param WC_Order $order Order object.
	 * @return string HTML badge markup.
	 */
	private static function get_badge_html( $order ) {
		$status = $order->get_meta( '_ccall_status' );
		if ( empty( $status ) ) {
			$triggered = $order->get_meta( '_ccall_call_triggered' );
			if ( 'yes' === $triggered ) {
				$status = 'queued';
			}
		}

		$badges = array(
			'queued'    => array( 'label' => __( 'Queued', 'ccall-ivr' ),    'class' => 'ccall-badge-blue' ),
			'calling'   => array( 'label' => __( 'Calling', 'ccall-ivr' ),   'class' => 'ccall-badge-yellow' ),
			'confirmed' => array( 'label' => __( 'Confirmed', 'ccall-ivr' ), 'class' => 'ccall-badge-green' ),
			'cancelled' => array( 'label' => __( 'Cancelled', 'ccall-ivr' ), 'class' => 'ccall-badge-red' ),
			'no_answer' => array( 'label' => __( 'No Answer', 'ccall-ivr' ), 'class' => 'ccall-badge-orange' ),
			'failed'    => array( 'label' => __( 'Failed', 'ccall-ivr' ),    'class' => 'ccall-badge-red' ),
		);

		if ( empty( $status ) || ! isset( $badges[ $status ] ) ) {
			return '<span class="ccall-badge ccall-badge-gray">' . esc_html__( '—', 'ccall-ivr' ) . '</span>';
		}

		$badge = $badges[ $status ];
		return '<span class="ccall-badge ' . esc_attr( $badge['class'] ) . '">' . esc_html( $badge['label'] ) . '</span>';
	}

	/**
	 * Inject inline CSS for badge styling.
	 */
	public static function badge_styles() {
		$screen = get_current_screen();
		if ( ! $screen ) {
			return;
		}

		$valid_screens = array( 'edit-shop_order', 'woocommerce_page_wc-orders' );
		if ( ! in_array( $screen->id, $valid_screens, true ) ) {
			return;
		}

		?>
		<style>
			.ccall-badge {
				display: inline-block;
				padding: 3px 10px;
				border-radius: 12px;
				font-size: 11px;
				font-weight: 600;
				line-height: 1.4;
				white-space: nowrap;
			}
			.ccall-badge-blue   { background: #dbeafe; color: #1d4ed8; }
			.ccall-badge-yellow { background: #fef3c7; color: #92400e; }
			.ccall-badge-green  { background: #d1fae5; color: #065f46; }
			.ccall-badge-red    { background: #fee2e2; color: #991b1b; }
			.ccall-badge-orange { background: #ffedd5; color: #9a3412; }
			.ccall-badge-gray   { background: #f1f5f9; color: #64748b; }

			.column-ccall_status { width: 100px; }
		</style>
		<?php
	}
}
