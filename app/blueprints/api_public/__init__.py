# E:\c-call-ivr\app\blueprints\api_public\__init__.py
"""Public REST API blueprint — POST /api/v1/orders etc. X-API-Key auth.
Full order endpoints land in Prompt 13 (Public API Refactor)."""
from __future__ import annotations

from flask import Blueprint

api_bp = Blueprint("api_public", __name__)

from app.blueprints.api_public import orders  # noqa: E402,F401
