# app/blueprints/customer/team.py
from __future__ import annotations

import logging
from flask import render_template, redirect, url_for, flash, request, jsonify, g
from flask_login import current_user
from app.blueprints.customer import customer_bp
from app.blueprints.auth.decorators import require_login, require_role
from app.db import db
from app.models.tenant import get_tenant_by_id
from app.services import team_service

logger = logging.getLogger("ccall")


@customer_bp.route("/settings/team", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def team_settings():
    tenant_id = g.tenant_id
    
    # 1. Fetch current team members
    members = db.query(
        """
        SELECT u.id, u.email, m.role, m.joined_at, u.last_login_at
        FROM user_tenant_memberships m
        JOIN users u ON m.user_id = u.id
        WHERE m.tenant_id = %s AND m.is_active = true
        ORDER BY m.joined_at ASC
        """,
        (tenant_id,)
    )

    # 2. Fetch pending invitations
    pending_invites = db.query(
        """
        SELECT i.id, i.email, i.role, i.expires_at, u.email AS invited_by_email
        FROM team_invitations i
        JOIN users u ON i.invited_by = u.id
        WHERE i.tenant_id = %s AND i.used_at IS NULL AND i.expires_at > now()
        ORDER BY i.created_at DESC
        """,
        (tenant_id,)
    )

    # 3. Plan seat limits
    plan = db.query_one(
        "SELECT p.max_users, p.name FROM tenants t JOIN plans p ON t.plan_id = p.id WHERE t.id = %s",
        (tenant_id,)
    )
    max_users = plan["max_users"] if plan else 1
    plan_name = plan["name"] if plan else "Free"
    current_users_count = len(members)

    # 4. Other tenant memberships for switcher dropdown
    other_memberships = db.query(
        """
        SELECT m.tenant_id, t.business_name, m.role
        FROM user_tenant_memberships m
        JOIN tenants t ON m.tenant_id = t.id
        WHERE m.user_id = %s AND m.is_active = true AND m.tenant_id != %s
        """,
        (current_user.id, tenant_id)
    )

    return render_template(
        "customer/team.html",
        members=members,
        pending_invites=pending_invites,
        max_users=max_users,
        plan_name=plan_name,
        current_users_count=current_users_count,
        other_memberships=other_memberships
    )


@customer_bp.route("/api/team/invite", methods=["POST"])
@require_login
@require_role(["admin"])
def team_invite():
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "agent").strip()

    if not email:
        flash("Email address is required.", "error")
        return redirect(url_for("customer.team_settings"))

    if role not in ("admin", "agent", "viewer"):
        flash("Invalid role selected.", "error")
        return redirect(url_for("customer.team_settings"))

    try:
        team_service.invite_user(g.tenant_id, email, role, current_user.id)
        flash(f"Invitation sent to {email} successfully.", "success")
    except PermissionError as exc:
        flash(str(exc), "error")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception as exc:
        logger.error(f"Invite user failed: {exc}", exc_info=True)
        flash("Failed to send invitation. Please try again.", "error")

    return redirect(url_for("customer.team_settings"))


@customer_bp.route("/api/team/users/<int:user_id>/role", methods=["POST"])
@require_login
@require_role(["admin"])
def team_change_role(user_id: int):
    new_role = request.form.get("role", "").strip()
    if new_role not in ("admin", "agent", "viewer"):
        flash("Invalid role selected.", "error")
        return redirect(url_for("customer.team_settings"))

    try:
        team_service.change_role(g.tenant_id, user_id, new_role, current_user.id)
        flash("User role updated successfully.", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception as exc:
        logger.error(f"Change role failed: {exc}", exc_info=True)
        flash("Failed to update user role.", "error")

    return redirect(url_for("customer.team_settings"))


@customer_bp.route("/api/team/users/<int:user_id>", methods=["DELETE"])
@require_login
@require_role(["admin"])
def team_remove_user(user_id: int):
    try:
        team_service.remove_user(g.tenant_id, user_id, current_user.id)
        flash("User removed from team successfully.", "success")
        # Direct return empty for HTMX row delete, or instruct browser reload
        response = jsonify({"success": True})
        response.headers["HX-Refresh"] = "true"
        return response
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.error(f"Remove user failed: {exc}", exc_info=True)
        return jsonify({"success": False, "error": "Failed to remove user."}), 500


@customer_bp.route("/api/team/invite/<int:invite_id>/cancel", methods=["POST"])
@require_login
@require_role(["admin"])
def team_cancel_invite(invite_id: int):
    tenant_id = g.tenant_id
    db.execute("DELETE FROM team_invitations WHERE id = %s AND tenant_id = %s", (invite_id, tenant_id))
    flash("Invitation cancelled successfully.", "success")
    return redirect(url_for("customer.team_settings"))


@customer_bp.route("/settings/team/switch/<int:target_tenant_id>", methods=["POST"])
@require_login
def team_switch_tenant(target_tenant_id: int):
    user_id = current_user.id
    # Verify user has membership in target tenant
    membership = db.query_one(
        "SELECT role FROM user_tenant_memberships WHERE user_id = %s AND tenant_id = %s AND is_active = true",
        (user_id, target_tenant_id)
    )
    if not membership:
        flash("You do not have access to this tenant.", "error")
        return redirect(url_for("customer.team_settings"))

    # Update active tenant context on the users table
    db.execute(
        "UPDATE users SET tenant_id = %s, role = %s WHERE id = %s",
        (target_tenant_id, membership["role"], user_id)
    )
    
    flash(f"Switched tenant context successfully.", "success")
    return redirect(url_for("customer.dashboard"))
