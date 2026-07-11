# E:\c-call-ivr\app\blueprints\admin\__init__.py
"""Super admin blueprint — tenants, numbers, providers, revenue.
Full routes land in Prompt 15 (Admin Panel UI)."""
from __future__ import annotations

from flask import Blueprint

admin_bp = Blueprint("admin", __name__, template_folder="../../templates/admin")

from app.blueprints.admin import routes  # noqa: E402,F401
from app.blueprints.admin import routes_numbers  # noqa: E402,F401
