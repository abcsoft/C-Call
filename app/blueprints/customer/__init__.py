# E:\c-call-ivr\app\blueprints\customer\__init__.py
"""Customer (tenant) panel blueprint — dashboard, orders, wallet, scripts.
Full routes land in Prompt 14 (Customer Panel UI)."""
from __future__ import annotations

from flask import Blueprint

customer_bp = Blueprint("customer", __name__, template_folder="../../templates/customer")

from app.blueprints.customer import routes  # noqa: E402,F401
from app.blueprints.customer import retry  # noqa: E402,F401
from app.blueprints.customer import orders  # noqa: E402,F401
from app.blueprints.customer import settings  # noqa: E402,F401
from app.blueprints.customer import cdr  # noqa: E402,F401
from app.blueprints.customer import wallet  # noqa: E402,F401
from app.blueprints.customer import team  # noqa: E402,F401
from app.blueprints.customer import flows  # noqa: E402,F401
