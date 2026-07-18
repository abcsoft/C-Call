# app/exceptions.py
from __future__ import annotations

from decimal import Decimal

class InsufficientBalanceError(Exception):
    """Raised when a tenant's wallet does not have enough balance for a transaction."""
    def __init__(self, current_balance: Decimal | float | str | None, required_amount: Decimal | float | str | None, message: str | None = None):
        self.current_balance = Decimal(str(current_balance)) if current_balance is not None else None
        self.required_amount = Decimal(str(required_amount)) if required_amount is not None else None
        if message is None:
            if self.current_balance is not None and self.required_amount is not None:
                message = f"Insufficient wallet balance. Current: {self.current_balance}, Required: {self.required_amount}."
            else:
                message = "Insufficient wallet balance."
        super().__init__(message)


class TenantSuspendedError(Exception):
    """Raised when an operation is attempted on a suspended tenant."""
    def __init__(self, tenant_id: int, message: str | None = None):
        self.tenant_id = tenant_id
        if message is None:
            message = f"Tenant {tenant_id} is suspended."
        super().__init__(message)


class OrderAlreadyExistsError(Exception):
    """Raised when attempting to create an order with an external ID that already exists."""
    def __init__(self, external_order_id: str, message: str | None = None):
        self.external_order_id = external_order_id
        if message is None:
            message = f"Order with external ID '{external_order_id}' already exists."
        super().__init__(message)


class TTSCredentialMissingError(Exception):
    """Raised when a BYOK TTS provider is selected but the tenant has no API key configured."""
    def __init__(self, provider: str, message: str | None = None):
        self.provider = provider
        if message is None:
            message = f"API key for provider '{provider}' is not configured for this account."
        super().__init__(message)


class MaxRetriesReachedError(Exception):
    """Raised when an order has reached its maximum call retry attempts."""
    def __init__(self, order_id: int, message: str | None = None):
        self.order_id = order_id
        if message is None:
            message = f"Order {order_id} has reached the maximum number of retry attempts."
        super().__init__(message)


class TTSProviderNotAllowedError(Exception):
    """Raised when the tenant's plan does not allow the requested TTS provider."""
    def __init__(self, provider: str, tenant_id: int | None = None, message: str | None = None):
        self.provider = provider
        self.tenant_id = tenant_id
        if message is None:
            message = f"TTS provider '{provider}' is not available on your current plan."
        super().__init__(message)


class TTSQuotaExceededError(Exception):
    """Raised when the tenant's monthly platform TTS quota is exhausted."""
    def __init__(self, tenant_id: int | None = None, provider: str | None = None, message: str | None = None):
        self.tenant_id = tenant_id
        self.provider = provider
        if message is None:
            message = "Monthly TTS character quota has been reached. Quota resets on the 1st of next month."
        super().__init__(message)


class TTSCredentialModeError(Exception):
    """Raised when the tenant attempts a credential mode not allowed by their plan."""
    def __init__(self, mode: str, tenant_id: int | None = None, message: str | None = None):
        self.mode = mode
        self.tenant_id = tenant_id
        if message is None:
            message = f"Credential mode '{mode}' is not allowed on your current plan."
        super().__init__(message)


# ── Typed dispatch exceptions ─────────────────────────────────────────────────
# Used by queue_confirmation_call to communicate failure kind without
# embedding machine-readable codes in exception message strings.

class DispatchOrderNotFoundError(Exception):
    """Order or tenant not found for this dispatch."""
    error_code = "order_not_found"
    retryable = False

class DispatchTenantMismatchError(Exception):
    """Job tenant_id does not match order tenant_id."""
    error_code = "tenant_mismatch"
    retryable = False

class DispatchTerminalOrderError(Exception):
    """Order is already in a terminal status."""
    error_code = "terminal_order"
    retryable = False

class DispatchDuplicateCallError(Exception):
    """A call already exists for this attempt (crash-recovery guard fired)."""
    error_code = "duplicate_call_existing"
    retryable = False

class DispatchNoCallerIdError(Exception):
    """No active caller ID assigned to the tenant."""
    error_code = "no_caller_id"
    retryable = False

class DispatchInvalidFlowError(Exception):
    """Required IVR flow could not be resolved."""
    error_code = "invalid_flow"
    retryable = False

class DispatchSipUnavailableError(Exception):
    """SIP provider temporarily unavailable (transient)."""
    error_code = "sip_temporarily_unavailable"
    retryable = True

class DispatchProviderTimeoutError(Exception):
    """Provider timed out (transient)."""
    error_code = "provider_timeout"
    retryable = True

class DispatchDatabaseRetryableError(Exception):
    """DB serialization error or transient lock (transient)."""
    error_code = "database_retryable"
    retryable = True

class DispatchStartupRejectedError(Exception):
    """Call runner rejected startup (executor full or credentials invalid)."""
    error_code = "startup_rejected"
    retryable = True

class DispatchInternalError(Exception):
    """Unexpected internal error — retried up to max_attempts then permanent."""
    error_code = "internal_dispatch_error"
    retryable = True
