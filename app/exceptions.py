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


class MaxRetriesReachedError(Exception):
    """Raised when an order has reached its maximum call retry attempts."""
    def __init__(self, order_id: int, message: str | None = None):
        self.order_id = order_id
        if message is None:
            message = f"Order {order_id} has reached the maximum number of retry attempts."
        super().__init__(message)
