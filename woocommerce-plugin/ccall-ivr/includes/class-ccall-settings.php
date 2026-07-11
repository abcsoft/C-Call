<?php
/**
 * CCAll_Settings — Admin settings page and AJAX handlers.
 *
 * @package CCallIvr
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class CCAll_Settings {

	/**
	 * Register hooks.
	 */
	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'add_menu_page' ) );
		add_action( 'admin_init', array( __CLASS__, 'register_settings' ) );

		// AJAX handlers
		add_action( 'wp_ajax_ccall_test_connection', array( __CLASS__, 'ajax_test_connection' ) );
		add_action( 'wp_ajax_ccall_test_webhook', array( __CLASS__, 'ajax_test_webhook' ) );
		add_action( 'wp_ajax_ccall_test_call', array( __CLASS__, 'ajax_send_test_call' ) );
		add_action( 'wp_ajax_ccall_clear_logs', array( __CLASS__, 'ajax_clear_logs' ) );
	}

	/**
	 * Add submenu page under WooCommerce.
	 */
	public static function add_menu_page() {
		add_submenu_page(
			'woocommerce',
			__( 'C-Call IVR Settings', 'ccall-ivr' ),
			__( 'C-Call IVR', 'ccall-ivr' ),
			'manage_options',
			'ccall-ivr-settings',
			array( __CLASS__, 'render_settings_page' )
		);
	}

	/**
	 * Register plugin settings.
	 */
	public static function register_settings() {
		register_setting( 'ccall-ivr-group', 'ccall_enabled' );
		register_setting( 'ccall-ivr-group', 'ccall_api_url' );
		register_setting( 'ccall-ivr-group', 'ccall_api_key' );
		register_setting( 'ccall-ivr-group', 'ccall_webhook_secret' );
		register_setting( 'ccall-ivr-group', 'ccall_trigger_status' );
		register_setting( 'ccall-ivr-group', 'ccall_cod_only' );
		register_setting( 'ccall-ivr-group', 'ccall_allowed_payment_methods', array(
			'type'              => 'array',
			'default'           => array(),
			'sanitize_callback' => array( __CLASS__, 'sanitize_array' ),
		) );
		register_setting( 'ccall-ivr-group', 'ccall_map_confirmed' );
		register_setting( 'ccall-ivr-group', 'ccall_map_cancelled' );
		register_setting( 'ccall-ivr-group', 'ccall_map_no_answer' );
		register_setting( 'ccall-ivr-group', 'ccall_map_failed' );
	}

	/**
	 * Sanitize array options.
	 *
	 * @param mixed $value
	 * @return array
	 */
	public static function sanitize_array( $value ) {
		if ( ! is_array( $value ) ) {
			return array();
		}
		return array_map( 'sanitize_text_field', $value );
	}

	/**
	 * Render settings page.
	 */
	public static function render_settings_page() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}

		$webhook_url = get_rest_url( null, '/ccall/v1/webhook' );
		$statuses    = wc_get_order_statuses();

		// Fetch payment gateways
		$gateways = array();
		if ( class_exists( 'WooCommerce' ) && isset( WC()->payment_gateways ) ) {
			$gateways = WC()->payment_gateways->payment_gateways();
		}

		// Current options
		$cod_only        = get_option( 'ccall_cod_only' );
		$allowed_methods = get_option( 'ccall_allowed_payment_methods', array() );
		if ( ! is_array( $allowed_methods ) ) {
			$allowed_methods = array();
		}

		$map_confirmed = get_option( 'ccall_map_confirmed', 'processing' );
		$map_cancelled = get_option( 'ccall_map_cancelled', 'cancelled' );
		$map_no_answer = get_option( 'ccall_map_no_answer', '_keep' );
		$map_failed    = get_option( 'ccall_map_failed', '_keep' );
		?>
		<div class="wrap">
			<style>
				:root {
					--ccall-primary: #4f46e5;
					--ccall-primary-hover: #4338ca;
					--ccall-success: #10b981;
					--ccall-danger: #ef4444;
					--ccall-warning: #f59e0b;
					--ccall-bg: #f8fafc;
					--ccall-card-bg: #ffffff;
					--ccall-border: #e2e8f0;
					--ccall-text: #0f172a;
					--ccall-text-muted: #64748b;
				}

				.ccall-container {
					font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen-Sans, Ubuntu, Cantarell, "Helvetica Neue", sans-serif;
					max-width: 950px;
					margin: 20px 0;
					background: var(--ccall-bg);
					border: 1px solid var(--ccall-border);
					border-radius: 12px;
					box-shadow: 0 4px 20px rgba(0,0,0,0.05);
					overflow: hidden;
				}

				.ccall-header {
					background: linear-gradient(135deg, var(--ccall-primary), #6366f1);
					padding: 30px;
					color: #ffffff;
					border-bottom: 1px solid var(--ccall-border);
				}

				.ccall-header h1 {
					color: #ffffff;
					font-size: 26px;
					font-weight: 700;
					margin: 0 0 8px 0;
					padding: 0;
					line-height: 1.2;
				}

				.ccall-header p {
					margin: 0;
					font-size: 14px;
					opacity: 0.9;
				}

				.ccall-body {
					padding: 30px;
				}

				.ccall-tabs {
					display: flex;
					border-bottom: 2px solid var(--ccall-border);
					margin-bottom: 25px;
				}

				.ccall-tab {
					padding: 10px 20px;
					font-weight: 600;
					font-size: 14px;
					color: var(--ccall-text-muted);
					cursor: pointer;
					border-bottom: 2px solid transparent;
					margin-bottom: -2px;
					transition: all 0.3s ease;
				}

				.ccall-tab.active {
					color: var(--ccall-primary);
					border-bottom-color: var(--ccall-primary);
				}

				.ccall-card {
					background: var(--ccall-card-bg);
					border: 1px solid var(--ccall-border);
					border-radius: 8px;
					padding: 24px;
					margin-bottom: 25px;
					box-shadow: 0 2px 8px rgba(0,0,0,0.02);
				}

				.ccall-card h2 {
					font-size: 18px;
					font-weight: 600;
					margin-top: 0;
					margin-bottom: 20px;
					border-bottom: 1px solid var(--ccall-border);
					padding-bottom: 10px;
				}

				.ccall-form-row {
					display: flex;
					flex-direction: column;
					margin-bottom: 20px;
				}

				.ccall-form-row label {
					font-weight: 600;
					font-size: 14px;
					color: var(--ccall-text);
					margin-bottom: 8px;
				}

				.ccall-form-row input[type="text"],
				.ccall-form-row input[type="password"],
				.ccall-form-row select {
					padding: 10px 14px;
					border: 1px solid var(--ccall-border);
					border-radius: 6px;
					font-size: 14px;
					color: var(--ccall-text);
					background: #fff;
					max-width: 100%;
					width: 100%;
					transition: border-color 0.3s ease;
				}

				.ccall-form-row input:focus,
				.ccall-form-row select:focus {
					border-color: var(--ccall-primary);
					outline: none;
					box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1);
				}

				.ccall-copy-box {
					display: flex;
					align-items: center;
					gap: 10px;
				}

				.ccall-btn {
					background: var(--ccall-primary);
					color: #ffffff;
					border: none;
					border-radius: 6px;
					padding: 10px 20px;
					font-weight: 600;
					font-size: 14px;
					cursor: pointer;
					transition: all 0.3s ease;
				}

				.ccall-btn:hover {
					background: var(--ccall-primary-hover);
				}

				.ccall-btn-sec {
					background: transparent;
					color: var(--ccall-primary);
					border: 1px solid var(--ccall-primary);
					border-radius: 6px;
					padding: 9px 18px;
					font-weight: 600;
					font-size: 14px;
					cursor: pointer;
					transition: all 0.3s ease;
				}

				.ccall-btn-sec:hover {
					background: rgba(79, 70, 229, 0.05);
				}

				.ccall-btn:disabled, .ccall-btn-sec:disabled {
					opacity: 0.6;
					cursor: not-allowed;
				}

				.ccall-btn-danger {
					background: transparent;
					color: var(--ccall-danger);
					border: 1px solid var(--ccall-danger);
					border-radius: 6px;
					padding: 9px 18px;
					font-weight: 600;
					font-size: 14px;
					cursor: pointer;
					transition: all 0.3s ease;
				}

				.ccall-btn-danger:hover {
					background: rgba(239, 68, 68, 0.05);
				}

				.ccall-alert {
					padding: 12px 16px;
					border-radius: 6px;
					font-size: 14px;
					font-weight: 500;
					margin-top: 15px;
					display: none;
					animation: fadeIn 0.4s ease;
				}

				.ccall-alert.success {
					background: rgba(16, 185, 129, 0.1);
					color: var(--ccall-success);
					border: 1px solid rgba(16, 185, 129, 0.2);
				}

				.ccall-alert.error {
					background: rgba(239, 68, 68, 0.1);
					color: var(--ccall-danger);
					border: 1px solid rgba(239, 68, 68, 0.2);
				}

				/* Logs Table Styling */
				.ccall-logs-table {
					width: 100%;
					border-collapse: collapse;
					margin-top: 15px;
				}

				.ccall-logs-table th, .ccall-logs-table td {
					padding: 10px 12px;
					text-align: left;
					font-size: 13px;
					border-bottom: 1px solid var(--ccall-border);
				}

				.ccall-logs-table th {
					background: #f1f5f9;
					font-weight: 600;
					color: var(--ccall-text);
				}

				.ccall-logs-table tr:hover {
					background: #f8fafc;
				}

				.ccall-log-badge {
					display: inline-block;
					padding: 2px 8px;
					border-radius: 4px;
					font-size: 11px;
					font-weight: 600;
					text-transform: uppercase;
				}

				.ccall-log-info { background: #dbeafe; color: #1d4ed8; }
				.ccall-log-warning { background: #fef3c7; color: #d97706; }
				.ccall-log-error { background: #fee2e2; color: #dc2626; }

				.ccall-log-context-trigger {
					color: var(--ccall-primary);
					text-decoration: underline;
					cursor: pointer;
					font-size: 12px;
				}

				.ccall-log-context-data {
					background: #f1f5f9;
					padding: 10px;
					border-radius: 4px;
					font-family: monospace;
					font-size: 11px;
					white-space: pre-wrap;
					margin-top: 5px;
					border: 1px solid var(--ccall-border);
				}

				.ccall-logs-wrapper {
					max-height: 450px;
					overflow-y: auto;
					border: 1px solid var(--ccall-border);
					border-radius: 6px;
				}

				@keyframes fadeIn {
					from { opacity: 0; transform: translateY(-5px); }
					to { opacity: 1; transform: translateY(0); }
				}
			</style>

			<div class="ccall-container">
				<!-- Header -->
				<div class="ccall-header">
					<h1><?php esc_html_e( 'C-Call IVR Order Confirmation', 'ccall-ivr' ); ?></h1>
					<p><?php esc_html_e( 'COD Order Automated Calls and Verification Panel', 'ccall-ivr' ); ?></p>
				</div>

				<div class="ccall-body">
					<!-- Tabs -->
					<div class="ccall-tabs">
						<div class="ccall-tab active" data-target="ccall-pane-settings"><?php esc_html_e( 'Configuration Settings', 'ccall-ivr' ); ?></div>
						<div class="ccall-tab" data-target="ccall-pane-test"><?php esc_html_e( 'System Test Console', 'ccall-ivr' ); ?></div>
						<div class="ccall-tab" data-target="ccall-pane-logs"><?php esc_html_e( 'Diagnostic Logs', 'ccall-ivr' ); ?></div>
					</div>

					<!-- Settings Form Pane -->
					<div id="ccall-pane-settings" class="ccall-pane">
						<form method="post" action="options.php">
							<?php settings_fields( 'ccall-ivr-group' ); ?>
							
							<div class="ccall-card">
								<h2><?php esc_html_e( 'Integration Credentials', 'ccall-ivr' ); ?></h2>
								
								<div class="ccall-form-row">
									<label><?php esc_html_e( 'Enable Integration', 'ccall-ivr' ); ?></label>
									<div>
										<input type="checkbox" name="ccall_enabled" value="1" <?php checked( 1, get_option( 'ccall_enabled' ) ); ?> />
										<span class="description"><?php esc_html_e( 'Turn on automated IVR confirmation calls.', 'ccall-ivr' ); ?></span>
									</div>
								</div>

								<div class="ccall-form-row">
									<label for="ccall_api_url"><?php esc_html_e( 'C-Call Base API URL', 'ccall-ivr' ); ?></label>
									<input type="text" id="ccall_api_url" name="ccall_api_url" value="<?php echo esc_attr( get_option( 'ccall_api_url', 'https://app.ccall.com.bd/api/v1' ) ); ?>" class="regular-text" />
								</div>

								<div class="ccall-form-row">
									<label for="ccall_api_key"><?php esc_html_e( 'X-API-Key Credentials', 'ccall-ivr' ); ?></label>
									<input type="password" id="ccall_api_key" name="ccall_api_key" value="<?php echo esc_attr( get_option( 'ccall_api_key' ) ); ?>" class="regular-text" autocomplete="new-password" />
								</div>
							</div>

							<div class="ccall-card">
								<h2><?php esc_html_e( 'Trigger Options & Webhooks', 'ccall-ivr' ); ?></h2>

								<div class="ccall-form-row">
									<label for="ccall_trigger_status"><?php esc_html_e( 'Trigger Order Status', 'ccall-ivr' ); ?></label>
									<select id="ccall_trigger_status" name="ccall_trigger_status">
										<?php
										$selected_trigger = get_option( 'ccall_trigger_status', 'pending' );
										foreach ( $statuses as $key => $label ) {
											$clean_key = str_replace( 'wc-', '', $key );
											echo '<option value="' . esc_attr( $clean_key ) . '" ' . selected( $selected_trigger, $clean_key, false ) . '>' . esc_html( $label ) . '</option>';
										}
										?>
									</select>
									<span class="description"><?php esc_html_e( 'Call triggers automatically when order reaches this status.', 'ccall-ivr' ); ?></span>
								</div>

								<div class="ccall-form-row">
									<label><?php esc_html_e( 'Only Trigger for Cash on Delivery (COD) Orders', 'ccall-ivr' ); ?></label>
									<div>
										<input type="checkbox" name="ccall_cod_only" value="1" <?php checked( 1, $cod_only ); ?> />
										<span class="description"><?php esc_html_e( 'If enabled, calls will only be triggered if the order payment method is COD.', 'ccall-ivr' ); ?></span>
									</div>
								</div>

								<div class="ccall-form-row">
									<label for="ccall_allowed_payment_methods"><?php esc_html_e( 'Allowed Payment Methods', 'ccall-ivr' ); ?></label>
									<select id="ccall_allowed_payment_methods" name="ccall_allowed_payment_methods[]" multiple style="height: 120px; max-width: 450px;">
										<?php
										if ( ! empty( $gateways ) ) {
											foreach ( $gateways as $gateway ) {
												$gateway_title = $gateway->get_title() ?: $gateway->get_method_title();
												$gateway_id    = $gateway->id;
												$is_selected   = in_array( $gateway_id, $allowed_methods, true );
												echo '<option value="' . esc_attr( $gateway_id ) . '" ' . selected( $is_selected, true, false ) . '>' . esc_html( $gateway_title ) . ' (' . esc_html( $gateway_id ) . ')</option>';
											}
										} else {
											echo '<option value="">' . esc_html__( 'No payment gateways found', 'ccall-ivr' ) . '</option>';
										}
										?>
									</select>
									<span class="description"><?php esc_html_e( 'Select which payment methods are allowed to trigger calls. Hold Ctrl (or Cmd) to select multiple. Leave empty to allow all methods.', 'ccall-ivr' ); ?></span>
								</div>

								<div class="ccall-form-row">
									<label for="ccall_webhook_secret"><?php esc_html_e( 'C-Call Webhook Secret', 'ccall-ivr' ); ?></label>
									<input type="password" id="ccall_webhook_secret" name="ccall_webhook_secret" value="<?php echo esc_attr( get_option( 'ccall_webhook_secret' ) ); ?>" class="regular-text" autocomplete="new-password" />
									<span class="description"><?php esc_html_e( 'Copy this value from your C-Call dashboard → Integration page → Webhook Secret. This must match exactly — even one character difference will cause all status updates to fail silently.', 'ccall-ivr' ); ?></span>
								</div>

								<div class="ccall-form-row">
									<label><?php esc_html_e( 'Your Store Webhook URL', 'ccall-ivr' ); ?></label>
									<div class="ccall-copy-box">
										<input type="text" id="ccall_webhook_url" value="<?php echo esc_url( $webhook_url ); ?>" readonly class="regular-text" style="background:#f1f5f9; width: 450px;" />
										<button type="button" class="ccall-btn-sec" onclick="copyWebhookUrl()"><?php esc_html_e( 'Copy Link', 'ccall-ivr' ); ?></button>
									</div>
									<span class="description"><?php esc_html_e( 'Provide this URL in your C-Call dashboard options.', 'ccall-ivr' ); ?></span>
								</div>
							</div>

							<div class="ccall-card">
								<h2><?php esc_html_e( 'Configurable Status Mappings', 'ccall-ivr' ); ?></h2>
								<p class="description"><?php esc_html_e( 'Map C-Call call outcomes to WooCommerce order statuses.', 'ccall-ivr' ); ?></p>
								<br>

								<div class="ccall-form-row">
									<label for="ccall_map_confirmed"><?php esc_html_e( 'On CONFIRMED Call', 'ccall-ivr' ); ?></label>
									<select id="ccall_map_confirmed" name="ccall_map_confirmed">
										<?php
										foreach ( $statuses as $key => $label ) {
											$clean_key = str_replace( 'wc-', '', $key );
											echo '<option value="' . esc_attr( $clean_key ) . '" ' . selected( $map_confirmed, $clean_key, false ) . '>' . esc_html( $label ) . '</option>';
										}
										?>
									</select>
								</div>

								<div class="ccall-form-row">
									<label for="ccall_map_cancelled"><?php esc_html_e( 'On CANCELLED Call', 'ccall-ivr' ); ?></label>
									<select id="ccall_map_cancelled" name="ccall_map_cancelled">
										<?php
										foreach ( $statuses as $key => $label ) {
											$clean_key = str_replace( 'wc-', '', $key );
											echo '<option value="' . esc_attr( $clean_key ) . '" ' . selected( $map_cancelled, $clean_key, false ) . '>' . esc_html( $label ) . '</option>';
										}
										?>
									</select>
								</div>

								<div class="ccall-form-row">
									<label for="ccall_map_no_answer"><?php esc_html_e( 'On NO ANSWER / UNREACHABLE Call', 'ccall-ivr' ); ?></label>
									<select id="ccall_map_no_answer" name="ccall_map_no_answer">
										<option value="_keep" <?php selected( $map_no_answer, '_keep' ); ?>><?php esc_html_e( 'Keep Current Status', 'ccall-ivr' ); ?></option>
										<?php
										foreach ( $statuses as $key => $label ) {
											$clean_key = str_replace( 'wc-', '', $key );
											echo '<option value="' . esc_attr( $clean_key ) . '" ' . selected( $map_no_answer, $clean_key, false ) . '>' . esc_html( $label ) . '</option>';
										}
										?>
									</select>
								</div>

								<div class="ccall-form-row">
									<label for="ccall_map_failed"><?php esc_html_e( 'On FAILED Call', 'ccall-ivr' ); ?></label>
									<select id="ccall_map_failed" name="ccall_map_failed">
										<option value="_keep" <?php selected( $map_failed, '_keep' ); ?>><?php esc_html_e( 'Keep Current Status', 'ccall-ivr' ); ?></option>
										<?php
										foreach ( $statuses as $key => $label ) {
											$clean_key = str_replace( 'wc-', '', $key );
											echo '<option value="' . esc_attr( $clean_key ) . '" ' . selected( $map_failed, $clean_key, false ) . '>' . esc_html( $label ) . '</option>';
										}
										?>
									</select>
								</div>
							</div>

							<?php submit_button( __( 'Save Configuration', 'ccall-ivr' ), 'ccall-btn', 'submit', false ); ?>
						</form>
					</div>

					<!-- Test Console Pane -->
					<div id="ccall-pane-test" class="ccall-pane" style="display:none;">
						<div class="ccall-card">
							<h2><?php esc_html_e( 'Connection Health Diagnostics', 'ccall-ivr' ); ?></h2>
							<p><?php esc_html_e( 'Verify network route verification with C-Call API servers.', 'ccall-ivr' ); ?></p>
							<button type="button" id="ccall-test-connection" class="ccall-btn-sec"><?php esc_html_e( 'Test Connection', 'ccall-ivr' ); ?></button>
							<div id="ccall-conn-result" class="ccall-alert"></div>
						</div>

						<div class="ccall-card">
							<h2><?php esc_html_e( 'Webhook Endpoint Diagnostics', 'ccall-ivr' ); ?></h2>
							<p><?php esc_html_e( 'Send a test POST order to /api/v1/orders using saved credentials to verify the webhook settings.', 'ccall-ivr' ); ?></p>
							<button type="button" id="ccall-test-webhook" class="ccall-btn-sec"><?php esc_html_e( 'Test Webhook', 'ccall-ivr' ); ?></button>
							<div id="ccall-webhook-result" class="ccall-alert"></div>
						</div>

						<div class="ccall-card">
							<h2><?php esc_html_e( 'Send Test Confirmation Call', 'ccall-ivr' ); ?></h2>
							<p><?php esc_html_e( 'Dispatch a simulated call request. C-Call will initiate a trial call and direct webhook outcomes back to your store.', 'ccall-ivr' ); ?></p>
							
							<form id="ccall-test-call-form">
								<div class="ccall-form-row">
									<label for="ccall_test_phone"><?php esc_html_e( 'Recipient Phone Number', 'ccall-ivr' ); ?></label>
									<input type="text" id="ccall_test_phone" placeholder="e.g. 01712345678" required class="regular-text" />
								</div>
								<button type="submit" class="ccall-btn"><?php esc_html_e( 'Send Test Call', 'ccall-ivr' ); ?></button>
							</form>
							
							<div id="ccall-call-result" class="ccall-alert"></div>
						</div>
					</div>

					<!-- Diagnostic Logs Pane -->
					<div id="ccall-pane-logs" class="ccall-pane" style="display:none;">
						<div class="ccall-card">
							<div style="display:flex; justify-content:space-between; align-items:center; border-bottom: 1px solid var(--ccall-border); padding-bottom:10px; margin-bottom:20px;">
								<h2 style="margin:0; border:none; padding:0;"><?php esc_html_e( 'System Event Logs', 'ccall-ivr' ); ?></h2>
								<button type="button" id="ccall-clear-logs" class="ccall-btn-danger" data-nonce="<?php echo esc_attr( wp_create_nonce( 'ccall_clear_logs_nonce' ) ); ?>">
									<?php esc_html_e( 'Clear Logs', 'ccall-ivr' ); ?>
								</button>
							</div>
							<p><?php esc_html_e( 'Showing the last 50 system and integration events chronologically.', 'ccall-ivr' ); ?></p>

							<div class="ccall-logs-wrapper">
								<table class="ccall-logs-table">
									<thead>
										<tr>
											<th style="width:20%;"><?php esc_html_e( 'Timestamp', 'ccall-ivr' ); ?></th>
											<th style="width:15%;"><?php esc_html_e( 'Level', 'ccall-ivr' ); ?></th>
											<th style="width:45%;"><?php esc_html_e( 'Message', 'ccall-ivr' ); ?></th>
											<th style="width:20%;"><?php esc_html_e( 'Details', 'ccall-ivr' ); ?></th>
										</tr>
									</thead>
									<tbody id="ccall-logs-tbody">
										<?php
										$log_entries = array_reverse( CCAll_Logger::get_entries() ); // Newest first
										if ( empty( $log_entries ) ) {
											echo '<tr class="no-logs-row"><td colspan="4" style="text-align:center; padding:20px; color:var(--ccall-text-muted);">' . esc_html__( 'No log entries found.', 'ccall-ivr' ) . '</td></tr>';
										} else {
											foreach ( $log_entries as $index => $entry ) {
												$level_class = 'ccall-log-info';
												if ( 'warning' === $entry['level'] ) {
													$level_class = 'ccall-log-warning';
												} elseif ( 'error' === $entry['level'] ) {
													$level_class = 'ccall-log-error';
												}
												?>
												<tr>
													<td><?php echo esc_html( $entry['timestamp'] ); ?></td>
													<td>
														<span class="ccall-log-badge <?php echo esc_attr( $level_class ); ?>">
															<?php echo esc_html( $entry['level'] ); ?>
														</span>
													</td>
													<td><?php echo esc_html( $entry['message'] ); ?></td>
													<td>
														<?php if ( ! empty( $entry['context'] ) ) : ?>
															<span class="ccall-log-context-trigger" onclick="toggleContext(<?php echo esc_attr( $index ); ?>)"><?php esc_html_e( 'Toggle Details', 'ccall-ivr' ); ?></span>
															<div id="ccall-context-<?php echo esc_attr( $index ); ?>" class="ccall-log-context-data" style="display:none;"><?php echo esc_html( wp_json_encode( $entry['context'], JSON_PRETTY_PRINT ) ); ?></div>
														<?php else : ?>
															—
														<?php endif; ?>
													</td>
												</tr>
												<?php
											}
										}
										?>
									</tbody>
								</table>
							</div>
							<div id="ccall-clear-result" class="ccall-alert" style="margin-top:15px;"></div>
						</div>
					</div>
				</div>
			</div>
		</div>

		<!-- UI Control JavaScript -->
		<script>
			function copyWebhookUrl() {
				var copyText = document.getElementById("ccall_webhook_url");
				copyText.select();
				copyText.setSelectionRange(0, 99999);
				navigator.clipboard.writeText(copyText.value);
				alert("Webhook URL copied successfully!");
			}

			function toggleContext(index) {
				var el = document.getElementById("ccall-context-" + index);
				if (el) {
					el.style.display = el.style.display === 'none' ? 'block' : 'none';
				}
			}

			jQuery(document).ready(function($) {
				// Tab toggle controls
				$('.ccall-tab').on('click', function() {
					$('.ccall-tab').removeClass('active');
					$(this).addClass('active');
					$('.ccall-pane').hide();
					$('#' + $(this).data('target')).show();
				});

				// AJAX: Test connection
				$('#ccall-test-connection').on('click', function(e) {
					e.preventDefault();
					var btn = $(this);
					btn.prop('disabled', true).text('Testing connection...');
					$('#ccall-conn-result').hide().removeClass('success error');

					$.post(ajaxurl, {
						action: 'ccall_test_connection',
						nonce: '<?php echo esc_js( wp_create_nonce( 'ccall_ajax_nonce' ) ); ?>'
					}, function(res) {
						btn.prop('disabled', false).text('Test Connection');
						if (res.success) {
							$('#ccall-conn-result').addClass('success').text('✓ ' + res.data.message).show();
						} else {
							var msg = res.data ? res.data.message : 'Error resolving request.';
							$('#ccall-conn-result').addClass('error').text('✗ ' + msg).show();
						}
					});
				});

				// AJAX: Test webhook
				$('#ccall-test-webhook').on('click', function(e) {
					e.preventDefault();
					var btn = $(this);
					btn.prop('disabled', true).text('Testing Webhook...');
					$('#ccall-webhook-result').hide().removeClass('success error');

					$.post(ajaxurl, {
						action: 'ccall_test_webhook',
						nonce: '<?php echo esc_js( wp_create_nonce( 'ccall_ajax_nonce' ) ); ?>'
					}, function(res) {
						btn.prop('disabled', false).text('Test Webhook');
						if (res.success) {
							$('#ccall-webhook-result').addClass('success').text('✓ ' + res.data.message + ' | Body: ' + res.data.body).show();
						} else {
							var msg = res.data ? res.data.message : 'Error resolving request.';
							$('#ccall-webhook-result').addClass('error').text('✗ ' + msg).show();
						}
					});
				});

				// AJAX: Dispatch test call
				$('#ccall-test-call-form').on('submit', function(e) {
					e.preventDefault();
					var form = $(this);
					var btn = form.find('button[type="submit"]');
					btn.prop('disabled', true).text('Sending call...');
					$('#ccall-call-result').hide().removeClass('success error');

					$.post(ajaxurl, {
						action: 'ccall_test_call',
						nonce: '<?php echo esc_js( wp_create_nonce( 'ccall_ajax_nonce' ) ); ?>',
						phone: $('#ccall_test_phone').val()
					}, function(res) {
						btn.prop('disabled', false).text('Send Test Call');
						if (res.success) {
							$('#ccall-call-result').addClass('success').text('✓ Call placed! Call ID: ' + res.data.call_id).show();
						} else {
							var msg = res.data ? res.data.message : 'Error resolving request.';
							$('#ccall-call-result').addClass('error').text('✗ ' + msg).show();
						}
					});
				});

				// AJAX: Clear Logs
				$('#ccall-clear-logs').on('click', function(e) {
					e.preventDefault();
					if ( ! confirm( 'Are you sure you want to clear all diagnostic logs?' ) ) {
						return;
					}
					var btn = $(this);
					btn.prop('disabled', true).text('Clearing...');
					$('#ccall-clear-result').hide().removeClass('success error');

					$.post(ajaxurl, {
						action: 'ccall_clear_logs',
						nonce: btn.data('nonce')
					}, function(res) {
						btn.prop('disabled', false).text('Clear Logs');
						if (res.success) {
							$('#ccall-clear-result').addClass('success').text('✓ ' + res.data.message).show();
							$('#ccall-logs-tbody').html('<tr class="no-logs-row"><td colspan="4" style="text-align:center; padding:20px; color:var(--ccall-text-muted);"><?php echo esc_js( __( 'No log entries found.', 'ccall-ivr' ) ); ?></td></tr>');
						} else {
							var msg = res.data ? res.data.message : 'Error clearing logs.';
							$('#ccall-clear-result').addClass('error').text('✗ ' + msg).show();
						}
					});
				});
			});
		</script>
		<?php
	}

	/**
	 * AJAX Handler: Verifies server connection to the C-Call API.
	 */
	public static function ajax_test_connection() {
		check_ajax_referer( 'ccall_ajax_nonce', 'nonce' );

		if ( ! current_user_can( 'manage_options' ) ) {
			wp_send_json_error( array( 'message' => __( 'Insufficient permissions.', 'ccall-ivr' ) ), 403 );
		}

		$api_url = get_option( 'ccall_api_url', 'https://app.ccall.com.bd/api/v1' );
		$api_key = get_option( 'ccall_api_key' );

		$health_url = esc_url_raw( trailingslashit( $api_url ) . 'health' );

		$response = wp_remote_get(
			$health_url,
			array(
				'headers' => array(
					'X-API-Key' => $api_key,
				),
				'timeout' => 10,
			)
		);

		if ( is_wp_error( $response ) ) {
			wp_send_json_error( array( 'message' => $response->get_error_message() ) );
		}

		$code = wp_remote_retrieve_response_code( $response );
		if ( 200 === $code ) {
			wp_send_json_success( array( 'message' => __( 'Connection parameters verified.', 'ccall-ivr' ) ) );
		} else {
			wp_send_json_error( array( 'message' => sprintf( __( 'API returned HTTP %d', 'ccall-ivr' ), $code ) ) );
		}
	}

	/**
	 * AJAX Handler: Sends a test POST to /api/v1/orders to diagnose webhooks.
	 */
	public static function ajax_test_webhook() {
		check_ajax_referer( 'ccall_ajax_nonce', 'nonce' );

		if ( ! current_user_can( 'manage_options' ) ) {
			wp_send_json_error( array( 'message' => __( 'Insufficient permissions.', 'ccall-ivr' ) ), 403 );
		}

		$api_url = get_option( 'ccall_api_url', 'https://app.ccall.com.bd/api/v1' );
		$api_key = get_option( 'ccall_api_key' );

		$body = array(
			'order_id'        => 'TEST-WH-' . random_int( 10000, 99999 ),
			'phone'           => '01700000000',
			'customer_name'   => 'Test Webhook',
			'amount'          => '100.00',
			'site_name'       => get_bloginfo( 'name' ),
			'store_name'      => get_bloginfo( 'name' ),
			'product_name'    => 'Test Webhook Product',
			'products'        => array(
				array( 'name' => 'Test Webhook Product', 'qty' => 1, 'total' => '100.00' ),
			),
			'items'           => 'Test Webhook Product',
			'subtotal'        => '100.00',
			'delivery_charge' => '60.00',
			'grand_total'     => '160.00',
			'price'           => '100.00',
			'payment_method'  => 'cod',
			'webhook_url'     => get_rest_url( null, '/ccall/v1/webhook' ),
		);

		$response = wp_remote_post(
			esc_url_raw( trailingslashit( $api_url ) . 'orders' ),
			array(
				'headers' => array(
					'Content-Type' => 'application/json',
					'X-API-Key'    => $api_key,
				),
				'body'    => wp_json_encode( $body ),
				'timeout' => 15,
			)
		);

		if ( is_wp_error( $response ) ) {
			wp_send_json_error( array( 'message' => $response->get_error_message() ) );
		}

		$code = wp_remote_retrieve_response_code( $response );
		$resp = wp_remote_retrieve_body( $response );

		wp_send_json_success( array(
			'message' => sprintf( __( 'HTTP Response Code: %d', 'ccall-ivr' ), $code ),
			'body'    => $resp
		) );
	}

	/**
	 * AJAX Handler: Dispatches a simulated order to C-Call.
	 */
	public static function ajax_send_test_call() {
		check_ajax_referer( 'ccall_ajax_nonce', 'nonce' );

		if ( ! current_user_can( 'manage_options' ) ) {
			wp_send_json_error( array( 'message' => __( 'Insufficient permissions.', 'ccall-ivr' ) ), 403 );
		}

		$phone   = isset( $_POST['phone'] ) ? sanitize_text_field( wp_unslash( $_POST['phone'] ) ) : '';
		$api_key = get_option( 'ccall_api_key' );
		$api_url = get_option( 'ccall_api_url', 'https://app.ccall.com.bd/api/v1' );

		if ( empty( $phone ) ) {
			wp_send_json_error( array( 'message' => __( 'Phone number is required.', 'ccall-ivr' ) ) );
		}

		if ( empty( $api_key ) || empty( $api_url ) ) {
			wp_send_json_error( array( 'message' => __( 'API credentials are not set.', 'ccall-ivr' ) ) );
		}

		$webhook_url = get_rest_url( null, '/ccall/v1/webhook' );

		$body = array(
			'order_id'        => 'TEST-' . random_int( 10000, 99999 ),
			'phone'           => $phone,
			'customer_name'   => 'Test Customer Name',
			'amount'          => '1650.00',
			'site_name'       => get_bloginfo( 'name' ),
			'store_name'      => get_bloginfo( 'name' ),
			'product_name'    => 'টেস্ট প্রোডাক্ট',
			'products'        => array(
				array( 'name' => 'টেস্ট প্রোডাক্ট', 'qty' => 1, 'total' => '1500.00' ),
			),
			'items'           => 'টেস্ট প্রোডাক্ট',
			'subtotal'        => '1500.00',
			'delivery_charge' => '150.00',
			'grand_total'     => '1650.00',
			'price'           => '1500.00',
			'payment_method'  => 'ক্যাশ অন ডেলিভারি',
			'webhook_url'     => $webhook_url,
		);

		$response = wp_remote_post(
			esc_url_raw( trailingslashit( $api_url ) . 'orders' ),
			array(
				'headers' => array(
					'Content-Type' => 'application/json',
					'X-API-Key'    => $api_key,
				),
				'body'    => wp_json_encode( $body ),
				'timeout' => 15,
			)
		);

		if ( is_wp_error( $response ) ) {
			wp_send_json_error( array( 'message' => $response->get_error_message() ) );
		}

		$code = wp_remote_retrieve_response_code( $response );
		$resp = wp_remote_retrieve_body( $response );

		if ( $code >= 200 && $code < 300 ) {
			$data = json_decode( $resp, true );
			wp_send_json_success( array( 'call_id' => isset( $data['call_id'] ) ? $data['call_id'] : 'Queued' ) );
		} else {
			wp_send_json_error( array( 'message' => sprintf( __( 'API returned HTTP %d: %s', 'ccall-ivr' ), $code, $resp ) ) );
		}
	}

	/**
	 * AJAX Handler: Clears the diagnostic logs.
	 */
	public static function ajax_clear_logs() {
		check_ajax_referer( 'ccall_clear_logs_nonce', 'nonce' );

		if ( ! current_user_can( 'manage_options' ) ) {
			wp_send_json_error( array( 'message' => __( 'Insufficient permissions.', 'ccall-ivr' ) ), 403 );
		}

		CCAll_Logger::clear();
		wp_send_json_success( array( 'message' => __( 'Diagnostic logs cleared.', 'ccall-ivr' ) ) );
	}
}
