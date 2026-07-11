<?php
/**
 * CCAll_Logger — Diagnostic event logger storing last 50 entries in wp_options.
 *
 * @package CCallIvr
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class CCAll_Logger {

	/** @var string Option key for the ring buffer. */
	const OPTION_KEY = 'ccall_event_log';

	/** @var int Maximum number of log entries kept. */
	const MAX_ENTRIES = 50;

	/**
	 * Log an event.
	 *
	 * @param string $level   One of: info, warning, error.
	 * @param string $message Human-readable log message.
	 * @param array  $context Optional associative array of extra data.
	 */
	public static function log( $level, $message, $context = array() ) {
		$entries = get_option( self::OPTION_KEY, array() );
		if ( ! is_array( $entries ) ) {
			$entries = array();
		}

		$entry = array(
			'timestamp' => current_time( 'mysql' ),
			'level'     => sanitize_key( $level ),
			'message'   => sanitize_text_field( $message ),
		);

		if ( ! empty( $context ) ) {
			// Sanitize context values — keep only scalars and arrays.
			$entry['context'] = self::sanitize_context( $context );
		}

		$entries[] = $entry;

		// Keep only the last MAX_ENTRIES.
		if ( count( $entries ) > self::MAX_ENTRIES ) {
			$entries = array_slice( $entries, -self::MAX_ENTRIES );
		}

		update_option( self::OPTION_KEY, $entries, false );
	}

	/**
	 * Shorthand: log info level.
	 *
	 * @param string $message Log message.
	 * @param array  $context Optional context.
	 */
	public static function info( $message, $context = array() ) {
		self::log( 'info', $message, $context );
	}

	/**
	 * Shorthand: log warning level.
	 *
	 * @param string $message Log message.
	 * @param array  $context Optional context.
	 */
	public static function warning( $message, $context = array() ) {
		self::log( 'warning', $message, $context );
	}

	/**
	 * Shorthand: log error level.
	 *
	 * @param string $message Log message.
	 * @param array  $context Optional context.
	 */
	public static function error( $message, $context = array() ) {
		self::log( 'error', $message, $context );
	}

	/**
	 * Retrieve all stored log entries (newest last).
	 *
	 * @return array List of log entry arrays.
	 */
	public static function get_entries() {
		$entries = get_option( self::OPTION_KEY, array() );
		return is_array( $entries ) ? $entries : array();
	}

	/**
	 * Clear all stored log entries.
	 */
	public static function clear() {
		update_option( self::OPTION_KEY, array(), false );
	}

	/**
	 * Sanitize context data — keep only scalars and simple arrays.
	 *
	 * @param array $context Raw context.
	 * @return array Sanitized context.
	 */
	private static function sanitize_context( $context ) {
		$clean = array();
		foreach ( $context as $key => $value ) {
			$key = sanitize_key( $key );
			if ( is_scalar( $value ) ) {
				$clean[ $key ] = $value;
			} elseif ( is_array( $value ) ) {
				$clean[ $key ] = array_map( 'sanitize_text_field', array_map( 'strval', $value ) );
			}
		}
		return $clean;
	}
}
