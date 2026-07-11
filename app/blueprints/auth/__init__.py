# E:\c-call-ivr\app\blueprints\auth\__init__.py
"""Auth blueprint — register/login/logout/verify/reset.
Full routes land in Prompt 03 (Auth Blueprint). This file wires the
blueprint + the Flask-Login user_loader so create_app() has something
real to register today."""
from __future__ import annotations

from flask import Blueprint

auth_bp = Blueprint("auth", __name__, template_folder="../../templates/auth")

from app.blueprints.auth import routes  # noqa: E402,F401
