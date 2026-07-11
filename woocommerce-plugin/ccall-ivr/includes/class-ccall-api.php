<?php
/**
 * CCAll_API — Handles outbound API communication with the C-Call IVR SaaS platform.
 *
 * @package CCallIvr
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class CCAll_API {

	/**
	 * Send an order confirmation call request to the C-Call API.
	 *
	 * @param int  $order_id WooCommerce Order ID.
	 * @param bool $force    If true, bypass the _ccall_call_triggered guard (used for resends).
	 * @return bool True on successful queueing, false otherwise.
	 */
	public static function send_order( $order_id, $force = false ) {
		// [CCALL STEP 1] WooCommerce order trigger fired
		CCAll_Logger::info( '[CCALL STEP 1] WooCommerce order trigger fired for Order ID: ' . $order_id );
		error_log( '[CCALL STEP 1] WooCommerce order trigger fired for Order ID: ' . $order_id );

		$order = wc_get_order( $order_id );
		if ( ! $order ) {
			return false;
		}

		// [CCALL STEP 2] Order ID, order status, payment method
		CCAll_Logger::info( sprintf( '[CCALL STEP 2] Order ID: %s, Status: %s, Payment Method: %s', $order->get_id(), $order->get_status(), $order->get_payment_method() ) );
		error_log( sprintf( '[CCALL STEP 2] Order ID: %s, Status: %s, Payment Method: %s', $order->get_id(), $order->get_status(), $order->get_payment_method() ) );

		// Guard: Prevent double-triggering unless forced (resend).
		if ( ! $force && 'yes' === $order->get_meta( '_ccall_call_triggered' ) ) {
			return false;
		}

		$api_key = get_option( 'ccall_api_key' );
		$api_url = get_option( 'ccall_api_url', 'https://app.ccall.com.bd/api/v1' );

		if ( empty( $api_key ) || empty( $api_url ) ) {
			$order->add_order_note( __( 'C-Call IVR: API credentials are not configured. Call was not queued.', 'ccall-ivr' ) );
			self::update_meta_on_failure( $order, 'API credentials not configured' );
			CCAll_Logger::error( 'API send failed: credentials missing', array( 'order' => $order_id ) );
			return false;
		}

		// Retrieve and normalize phone number to standard 11-digit format starting with 01.
		$phone = $order->get_billing_phone();
		$phone = preg_replace( '/\D/', '', $phone );

		if ( empty( $phone ) ) {
			$order->add_order_note( __( 'C-Call IVR Skip: Billing phone number is empty.', 'ccall-ivr' ) );
			self::update_meta_on_failure( $order, 'Phone number empty' );
			CCAll_Logger::warning( 'API send skipped: empty phone', array( 'order' => $order_id ) );
			return false;
		}

		if ( substr( $phone, 0, 4 ) === '8801' ) {
			$phone = substr( $phone, 2 );
		} elseif ( substr( $phone, 0, 5 ) === '00880' ) {
			$phone = substr( $phone, 4 );
		}

		if ( 10 === strlen( $phone ) && '1' === substr( $phone, 0, 1 ) ) {
			$phone = '0' . $phone;
		}

		// Gather order items with structured product data.
		$items = array();
		$items_str_parts = array();
		$products_array = array();
		foreach ( $order->get_items() as $item ) {
			$items_str_parts[] = $item->get_name();
			$products_array[] = array(
				'name'  => $item->get_name(),
				'qty'   => $item->get_quantity(),
				'total' => number_format( (float) $item->get_total(), 2, '.', '' ),
			);
		}
		$items_str = implode( ', ', $items_str_parts );

		// Smart product_name formatting: single item shows name, multiple shows summary
		$item_count = count( $items_str_parts );
		if ( 1 === $item_count ) {
			$product_name = $items_str_parts[0];
		} elseif ( $item_count > 1 ) {
			$product_name = $items_str_parts[0] . ' সহ মোট ' . $item_count . ' টি পণ্য';
		} else {
			$product_name = '';
		}

		$webhook_url = add_query_arg( 'rest_route', '/ccall/v1/webhook', home_url( '/' ) );

		// Calculate total quantity
		$total_qty = 0;
		foreach ( $order->get_items() as $item ) {
			$total_qty += $item->get_quantity();
		}
		// Fetch first product price as item_price
		$first_product = reset( $products_array );
		$item_price    = $first_product ? $first_product['total'] : '0.00';

		$body = array(
			'order_id'        => $order->get_id(), // Use numeric database ID
			'order_number'    => $order->get_order_number(),
			'phone'           => $phone,
			'customer_name'   => trim( $order->get_billing_first_name() . ' ' . $order->get_billing_last_name() ),
			'amount'          => number_format( (float) $order->get_total(), 2, '.', '' ),
			'site_name'       => get_bloginfo( 'name' ),
			'store_name'      => get_bloginfo( 'name' ),
			'product_name'    => $product_name,
			'products'        => $products_array,
			'qty'             => $total_qty,
			'item_price'      => $item_price,
			'subtotal'        => number_format( (float) $order->get_subtotal(), 2, '.', '' ),
			'delivery_charge' => number_format( (float) $order->get_shipping_total(), 2, '.', '' ),
			'discount'        => number_format( (float) $order->get_discount_total(), 2, '.', '' ),
			'tax'             => number_format( (float) $order->get_cart_tax(), 2, '.', '' ),
			'grand_total'     => number_format( (float) $order->get_total(), 2, '.', '' ),
			'price'           => number_format( (float) $order->get_subtotal(), 2, '.', '' ),
			'payment_method'  => $order->get_payment_method_title(),
			'site_url'        => site_url(),
			'webhook_url'     => $webhook_url,
		);

		// [CCALL STEP 3] Payload prepared
		CCAll_Logger::info( '[CCALL STEP 3] Payload prepared: ' . wp_json_encode( $body ) );
		error_log( '[CCALL STEP 3] Payload prepared: ' . wp_json_encode( $body ) );

		$dest_url = $force ? trailingslashit( $api_url ) . 'orders/' . $order->get_order_number() . '/recall' : trailingslashit( $api_url ) . 'orders';
		// [CCALL STEP 4] API URL called
		CCAll_Logger::info( '[CCALL STEP 4] API URL called: ' . $dest_url );
		error_log( '[CCALL STEP 4] API URL called: ' . $dest_url );

		$masked_api_key = empty( $api_key ) ? '' : substr( $api_key, 0, 4 ) . '...' . substr( $api_key, -4 );
		// [CCALL STEP 5] API request headers without exposing secret
		CCAll_Logger::info( '[CCALL STEP 5] API request headers: Content-Type: application/json, X-API-Key: ' . $masked_api_key );
		error_log( '[CCALL STEP 5] API request headers: Content-Type: application/json, X-API-Key: ' . $masked_api_key );

		if ( $force ) {
			$response = wp_remote_post(
				esc_url_raw( $dest_url ),
				array(
					'headers' => array(
						'Content-Type' => 'application/json',
						'X-API-Key'    => $api_key,
					),
					'body'    => wp_json_encode( $body ),
					'timeout' => 15,
				)
			);
		} else {
			$response = wp_remote_post(
				esc_url_raw( $dest_url ),
				array(
					'headers' => array(
						'Content-Type' => 'application/json',
						'X-API-Key'    => $api_key,
					),
					'body'    => wp_json_encode( $body ),
					'timeout' => 15,
				)
			);
		}

		if ( is_wp_error( $response ) ) {
			$error_message = $response->get_error_message();
			$order->add_order_note( sprintf( __( 'C-Call IVR Queue Failed: %s', 'ccall-ivr' ), $error_message ) );
			self::update_meta_on_failure( $order, $error_message );
			CCAll_Logger::error( 'API request failed', array( 'order' => $order_id, 'error' => $error_message ) );
			return false;
		}

		$code      = wp_remote_retrieve_response_code( $response );
		$resp_body = wp_remote_retrieve_body( $response );

		// [CCALL STEP 6] SaaS response HTTP code
		CCAll_Logger::info( '[CCALL STEP 6] SaaS response HTTP code: ' . $code );
		error_log( '[CCALL STEP 6] SaaS response HTTP code: ' . $code );

		// [CCALL STEP 7] SaaS response body
		CCAll_Logger::info( '[CCALL STEP 7] SaaS response body: ' . $resp_body );
		error_log( '[CCALL STEP 7] SaaS response body: ' . $resp_body );

		if ( $code >= 200 && $code < 300 ) {
			$resp_data = json_decode( $resp_body, true );

			// Persist order meta.
			$order->update_meta_data( '_ccall_call_triggered', 'yes' );
			$order->update_meta_data( '_ccall_status', 'queued' );
			$order->update_meta_data( '_ccall_last_error', '' );

			// Save call_id from response.
			$call_id = '';
			if ( isset( $resp_data['call_id'] ) && ! empty( $resp_data['call_id'] ) ) {
				$call_id = $resp_data['call_id'];
			} elseif ( isset( $resp_data['internal_id'] ) ) {
				$call_id = $resp_data['internal_id'];
			}
			if ( ! empty( $call_id ) ) {
				$order->update_meta_data( '_ccall_call_id', $call_id );
				// [CCALL STEP 8] Call ID/order UUID saved to order meta
				CCAll_Logger::info( '[CCALL STEP 8] Call ID saved to order meta: ' . $call_id );
				error_log( '[CCALL STEP 8] Call ID saved to order meta: ' . $call_id );
			}

			// Increment attempt count.
			$current_attempts = absint( $order->get_meta( '_ccall_attempt_count' ) );
			$order->update_meta_data( '_ccall_attempt_count', $current_attempts + 1 );

			$order->save();
			$order->add_order_note( __( 'C-Call IVR: Call queued successfully.', 'ccall-ivr' ) );
			CCAll_Logger::info( 'Call queued', array(
				'order'   => $order_id,
				'call_id' => $call_id,
				'attempt' => $current_attempts + 1,
			) );
			return true;
		} else {
			$order->add_order_note( sprintf( __( 'C-Call IVR Queue Failed (HTTP %d): %s', 'ccall-ivr' ), $code, $resp_body ) );
			self::update_meta_on_failure( $order, "HTTP {$code}" );
			CCAll_Logger::error( 'API queue failed', array( 'order' => $order_id, 'http_code' => $code ) );
			return false;
		}
	}

	/**
	 * Update order meta on failure.
	 *
	 * @param WC_Order $order         Order object.
	 * @param string   $error_message Error description.
	 */
	private static function update_meta_on_failure( $order, $error_message ) {
		$order->update_meta_data( '_ccall_status', 'failed' );
		$order->update_meta_data( '_ccall_last_error', sanitize_text_field( $error_message ) );
		$order->save();
	}

	/**
	 * Formats PHP associative arrays exactly like Python's json.dumps(..., sort_keys=True).
	 *
	 * @param mixed $data Data structure to serialize.
	 * @return string Serialized JSON string.
	 */
	public static function python_json_dumps( $data ) {
		if ( is_array( $data ) ) {
			$keys     = array_keys( $data );
			$is_assoc = ( ! empty( $data ) && array_keys( $data ) !== range( 0, count( $data ) - 1 ) );
			if ( empty( $data ) ) {
				return $is_assoc ? '{}' : '[]';
			}

			if ( $is_assoc ) {
				ksort( $data );
				$parts = array();
				foreach ( $data as $key => $value ) {
					$json_key = '"' . addcslashes( $key, "\"\t\n\r\0\x0b" ) . '"';
					$json_val = self::python_json_dumps( $value );
					$parts[]  = $json_key . ': ' . $json_val;
				}
				return '{' . implode( ', ', $parts ) . '}';
			} else {
				$parts = array();
				foreach ( $data as $value ) {
					$parts[] = self::python_json_dumps( $value );
				}
				return '[' . implode( ', ', $parts ) . ']';
			}
		} elseif ( is_string( $data ) ) {
			return '"' . addcslashes( $data, "\"\t\n\r\0\x0b" ) . '"';
		} elseif ( is_bool( $data ) ) {
			return $data ? 'true' : 'false';
		} elseif ( is_null( $data ) ) {
			return 'null';
		}
		return (string) $data;
	}
}
