<?php
/**
 * CCAll_Webhook — Handles inbound webhook processing from C-Call SaaS.
 *
 * Features:
 * - Timestamp-based HMAC-SHA256 verification with 5-minute window.
 * - Legacy HMAC fallback for backward compatibility.
 * - Full order meta persistence on every webhook.
 * - Duplicate webhook detection using composite key.
 * - Configurable status mappings.
 * - Diagnostic logging.
 *
 * @package CCallIvr
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class CCAll_Webhook {

	/**
	 * Register the REST API webhook endpoint.
	 */
	public static function register_route() {
		register_rest_route(
			'ccall/v1',
			'/webhook',
			array(
				'methods'             => 'POST',
				'callback'            => array( __CLASS__, 'handle' ),
				'permission_callback' => '__return_true',
			)
		);
	}

	/**
	 * Main webhook handler.
	 *
	 * @param WP_REST_Request $request Incoming request.
	 * @return WP_REST_Response
	 */
	public static function handle( $request ) {
		$raw_body = $request->get_body();
		$headers  = $request->get_headers();

		// [CCALL STEP 9] Webhook received from SaaS
		CCAll_Logger::info( '[CCALL STEP 9] Webhook received from SaaS. Body: ' . $raw_body );
		error_log( '[CCALL STEP 9] Webhook received from SaaS. Body: ' . $raw_body );

		// --- Step 1: HMAC Signature Verification ---
		$secret = get_option( 'ccall_webhook_secret' );
		$masked_secret = empty($secret) ? 'empty' : substr($secret, 0, 4) . '...' . substr($secret, -4);
		// [CCALL STEP 11] Webhook secret source used
		CCAll_Logger::info( '[CCALL STEP 11] Webhook secret source used: wp_options (ccall_webhook_secret) = ' . $masked_secret );
		error_log( '[CCALL STEP 11] Webhook secret source used: wp_options (ccall_webhook_secret) = ' . $masked_secret );

		if ( empty( $secret ) ) {
			CCAll_Logger::error( 'Webhook rejected: secret not configured' );
			return new WP_REST_Response( array( 'success' => false, 'message' => 'Webhook secret not configured' ), 500 );
		}

		$verify_result = self::verify_signature( $raw_body, $headers, $secret );
		if ( is_wp_error( $verify_result ) ) {
			$error_code = $verify_result->get_error_code();
			$error_msg = $verify_result->get_error_message();
			// [CCALL STEP 10] Webhook timestamp/signature check result
			CCAll_Logger::error( '[CCALL STEP 10] Webhook signature check result: FAILED. Reason: ' . $error_code . ' - ' . $error_msg );
			error_log( '[CCALL STEP 10] Webhook signature check result: FAILED. Reason: ' . $error_code . ' - ' . $error_msg );
			return new WP_REST_Response(
				array( 'success' => false, 'message' => $error_msg ),
				403
			);
		} else {
			// [CCALL STEP 10] Webhook timestamp/signature check result
			CCAll_Logger::info( '[CCALL STEP 10] Webhook signature check result: PASSED' );
			error_log( '[CCALL STEP 10] Webhook signature check result: PASSED' );
		}

		// --- Step 2: Parse body ---
		$data = json_decode( $raw_body, true );
		if ( ! is_array( $data ) ) {
			CCAll_Logger::error( 'Webhook rejected: invalid JSON body' );
			return new WP_REST_Response( array( 'success' => false, 'message' => 'Invalid JSON body' ), 400 );
		}

		// Extract fields.
		$ext_order_id  = isset( $data['order_id'] ) ? sanitize_text_field( $data['order_id'] ) : '';
		$order_status  = isset( $data['order_status'] ) ? sanitize_text_field( $data['order_status'] ) : '';
		$dtmf_digit    = isset( $data['dtmf_digit'] ) ? sanitize_text_field( $data['dtmf_digit'] ) : '';
		$talk_seconds  = isset( $data['talk_seconds'] ) ? intval( $data['talk_seconds'] ) : 0;
		$attempt_no    = isset( $data['attempt_no'] ) ? intval( $data['attempt_no'] ) : 1;
		$order_internal_id = isset( $data['order_internal_id'] ) ? sanitize_text_field( $data['order_internal_id'] ) : (isset( $data['internal_id'] ) ? sanitize_text_field( $data['internal_id'] ) : '');
		$call_id           = isset( $data['call_id'] ) ? sanitize_text_field( $data['call_id'] ) : $order_internal_id;
		$event         = isset( $data['event'] ) ? sanitize_text_field( $data['event'] ) : '';

		// [CCALL STEP 12] DTMF/status received
		CCAll_Logger::info( sprintf( '[CCALL STEP 12] DTMF/status received: Status = %s, DTMF = %s', $order_status, $dtmf_digit ) );
		error_log( sprintf( '[CCALL STEP 12] DTMF/status received: Status = %s, DTMF = %s', $order_status, $dtmf_digit ) );

		// --- Step 3: Locate WooCommerce order ---
		if ( empty( $ext_order_id ) ) {
			CCAll_Logger::error( 'Webhook rejected: missing order_id' );
			return new WP_REST_Response( array( 'success' => false, 'message' => 'Missing order_id' ), 400 );
		}

		$wc_order_id = function_exists( 'wc_get_order_id_by_order_number' )
			? wc_get_order_id_by_order_number( $ext_order_id )
			: 0;

		if ( ! $wc_order_id ) {
			$wc_order_id = absint( $ext_order_id );
		}

		$order = wc_get_order( $wc_order_id );
		if ( ! $order ) {
			CCAll_Logger::warning( 'Webhook: order not found', array( 'order_id' => $ext_order_id ) );
			return new WP_REST_Response( array( 'success' => false, 'message' => 'Order not found' ), 404 );
		}

		// --- Step 4: Deduplication ---
		$dedup_key     = "{$call_id}:{$order_status}:{$attempt_no}";
		$last_dedup    = $order->get_meta( '_ccall_last_webhook_key' );
		if ( $dedup_key === $last_dedup ) {
			CCAll_Logger::info( 'Webhook duplicate skipped', array( 'key' => $dedup_key, 'order' => $ext_order_id ) );
			return new WP_REST_Response( array( 'success' => true, 'message' => 'Duplicate webhook — already processed' ), 200 );
		}

		// --- Step 5: Save all meta ---
		$order->update_meta_data( '_ccall_status', strtolower( $order_status ) );
		$order->update_meta_data( '_ccall_dtmf_digit', $dtmf_digit );
		$order->update_meta_data( '_ccall_last_webhook_at', current_time( 'mysql' ) );
		$order->update_meta_data( '_ccall_last_webhook_key', $dedup_key );

		$order->update_meta_data( '_ccall_order_internal_id', $order_internal_id );
		$order->update_meta_data( '_ccall_call_id', $call_id );

		// Update attempt count — use webhook attempt_no if higher than stored.
		$stored_attempts = absint( $order->get_meta( '_ccall_attempt_count' ) );
		if ( $attempt_no > $stored_attempts ) {
			$order->update_meta_data( '_ccall_attempt_count', $attempt_no );
		}

		$order->save();

		// --- Step 6: Private order note ---
		$order->add_order_note(
			sprintf(
				/* translators: 1: event, 2: status, 3: DTMF digit, 4: talk seconds, 5: attempt number */
				__( 'C-Call Webhook [%1$s]: Status=%2$s, DTMF=%3$s, Duration=%4$ds, Attempt=%5$d', 'ccall-ivr' ),
				$event ?: 'unknown',
				$order_status,
				$dtmf_digit ?: '—',
				$talk_seconds,
				$attempt_no
			),
			false // is_customer_note = false → private note
		);

		// --- Step 7: Apply configurable status mapping ---
		$result_message = self::apply_status_mapping( $order, $order_status, $dtmf_digit, $talk_seconds );

		// [CCALL STEP 13] WooCommerce order status update result
		CCAll_Logger::info( '[CCALL STEP 13] WooCommerce order status update result: ' . $result_message );
		error_log( '[CCALL STEP 13] WooCommerce order status update result: ' . $result_message );

		CCAll_Logger::info( 'Webhook processed', array(
			'order'  => $ext_order_id,
			'status' => $order_status,
			'dtmf'   => $dtmf_digit,
			'result' => $result_message,
		) );

		return new WP_REST_Response( array( 'success' => true, 'message' => $result_message ), 200 );
	}

	/**
	 * Verify HMAC signature — supports both timestamp-based and legacy formats.
	 *
	 * @param string $raw_body  Raw request body.
	 * @param array  $headers   Request headers (WP normalizes to lowercase + underscores).
	 * @param string $secret    Webhook secret.
	 * @return true|WP_Error True on success, WP_Error on failure.
	 */
	private static function verify_signature( $raw_body, $headers, $secret ) {
		// Normalize header access — WP REST converts dashes to underscores and lowercases.
		$sig_header = '';
		if ( isset( $headers['x_c_call_signature'] ) ) {
			$sig_header = is_array( $headers['x_c_call_signature'] ) ? $headers['x_c_call_signature'][0] : $headers['x_c_call_signature'];
		} elseif ( isset( $headers['x_ccall_signature'] ) ) {
			$sig_header = is_array( $headers['x_ccall_signature'] ) ? $headers['x_ccall_signature'][0] : $headers['x_ccall_signature'];
		}

		if ( empty( $sig_header ) ) {
			return new WP_Error( 'missing_signature', 'Missing signature header' );
		}

		// Strip sha256= prefix if present.
		if ( 0 === strpos( $sig_header, 'sha256=' ) ) {
			$sig_header = substr( $sig_header, 7 );
		}

		// Check for timestamp-based verification.
		$timestamp = '';
		if ( isset( $headers['x_c_call_timestamp'] ) ) {
			$timestamp = is_array( $headers['x_c_call_timestamp'] ) ? $headers['x_c_call_timestamp'][0] : $headers['x_c_call_timestamp'];
		}

		if ( ! empty( $timestamp ) ) {
			// --- Timestamp-based HMAC ---
			$ts_int = absint( $timestamp );
			if ( abs( time() - $ts_int ) > 300 ) {
				return new WP_Error( 'expired', 'Webhook timestamp expired (>5 minutes)' );
			}

			// Clean payload if signature key is present
			$data = json_decode( $raw_body, true );
			if ( is_array( $data ) && isset( $data['signature'] ) ) {
				unset( $data['signature'] );
				$serialized = CCAll_API::python_json_dumps( $data );
			} else {
				$serialized = $raw_body;
			}

			$base_string = $timestamp . '.' . $serialized;
			$computed    = hash_hmac( 'sha256', $base_string, $secret );

			if ( ! hash_equals( $computed, $sig_header ) ) {
				return new WP_Error( 'invalid_signature', 'Timestamp-based signature mismatch' );
			}

			return true;
		}

		// --- Legacy HMAC: reconstruct Python json.dumps(payload, sort_keys=True) ---
		$data = json_decode( $raw_body, true );
		if ( ! is_array( $data ) ) {
			return new WP_Error( 'invalid_body', 'Cannot parse body for legacy signature' );
		}

		if ( isset( $data['signature'] ) ) {
			unset( $data['signature'] );
		}

		$serialized = CCAll_API::python_json_dumps( $data );
		$computed   = hash_hmac( 'sha256', $serialized, $secret );

		if ( ! hash_equals( $computed, $sig_header ) ) {
			return new WP_Error( 'invalid_signature', 'Legacy signature mismatch' );
		}

		return true;
	}

	/**
	 * Apply configurable WooCommerce status mapping based on C-Call outcome.
	 *
	 * @param WC_Order $order        WooCommerce order object.
	 * @param string   $ccall_status C-Call status (CONFIRMED, CANCELLED, NO_ANSWER, FAILED, etc).
	 * @param string   $dtmf_digit   DTMF digit pressed by customer.
	 * @param int      $talk_seconds Call duration in seconds.
	 * @return string Result message.
	 */
	private static function apply_status_mapping( $order, $ccall_status, $dtmf_digit, $talk_seconds ) {
		$map = array(
			'CONFIRMED'   => get_option( 'ccall_map_confirmed', 'processing' ),
			'CANCELLED'   => get_option( 'ccall_map_cancelled', 'cancelled' ),
			'NO_ANSWER'   => get_option( 'ccall_map_no_answer', '_keep' ),
			'UNREACHABLE' => get_option( 'ccall_map_no_answer', '_keep' ),
			'FAILED'      => get_option( 'ccall_map_failed', '_keep' ),
		);

		$target_status = isset( $map[ $ccall_status ] ) ? $map[ $ccall_status ] : '_keep';

		if ( '_keep' === $target_status || empty( $target_status ) ) {
			return 'Outcome logged (status unchanged)';
		}

		$note = '';
		switch ( $ccall_status ) {
			case 'CONFIRMED':
				$note = sprintf(
					__( 'C-Call IVR: Customer confirmed (DTMF: %1$s, Duration: %2$ds).', 'ccall-ivr' ),
					$dtmf_digit ?: '—',
					$talk_seconds
				);
				break;
			case 'CANCELLED':
				$note = sprintf(
					__( 'C-Call IVR: Customer cancelled (DTMF: %1$s, Duration: %2$ds).', 'ccall-ivr' ),
					$dtmf_digit ?: '—',
					$talk_seconds
				);
				break;
			default:
				$note = sprintf(
					__( 'C-Call IVR: Status changed to %1$s → mapped to WC status "%2$s".', 'ccall-ivr' ),
					$ccall_status,
					$target_status
				);
		}

		$order->update_status( $target_status, $note );

		return "Order status updated to {$target_status}";
	}
}
