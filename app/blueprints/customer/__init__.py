# E:\c-call-ivr\app\blueprints\customer\__init__.py
"""Customer (tenant) panel blueprint — dashboard, orders, wallet, scripts.
Full routes land in Prompt 14 (Customer Panel UI)."""
from __future__ import annotations

from flask import Blueprint, g
from flask_login import current_user

customer_bp = Blueprint("customer", __name__, template_folder="../../templates/customer")


@customer_bp.context_processor
def inject_subscription_banner():
    """Inject subscription status banner into every customer panel template."""
    banner = None
    try:
        tenant_id = getattr(g, "tenant_id", None)
        if tenant_id and current_user and current_user.is_authenticated and current_user.role != "super_admin":
            from app.services.subscription_access_service import get_tenant_status_banner
            banner = get_tenant_status_banner(tenant_id)
    except Exception:
        pass
    return {"subscription_banner": banner}


from app.blueprints.customer import routes  # noqa: E402,F401
from app.blueprints.customer import retry  # noqa: E402,F401
from app.blueprints.customer import orders  # noqa: E402,F401
from app.blueprints.customer import settings  # noqa: E402,F401
from app.blueprints.customer import cdr  # noqa: E402,F401
from app.blueprints.customer import wallet  # noqa: E402,F401
from app.blueprints.customer import team  # noqa: E402,F401
from app.blueprints.customer import flows  # noqa: E402,F401
