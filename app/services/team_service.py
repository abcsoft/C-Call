# app/services/team_service.py
from __future__ import annotations

import datetime
import json
import secrets
from typing import Any
import bcrypt
from app.db import db
from app.models.tenant import get_tenant_by_id
from app.models.user import create_user, get_user_by_email, get_user_by_id, User
from app.services.email_service import send_team_invite_email


def invite_user(tenant_id: int, email: str, role: str, invited_by: int) -> str:
    """Invite a user to a tenant by email.
    Checks user not already in this tenant; tenant within max_users plan limit.
    If user exists (different tenant): add them to this tenant too (multi-tenant user).
    If new user: create user (no password yet), send invite email with set-password link.
    Create team_invitations record (email, tenant_id, role, token, expires_at=48h).
    """
    # 1. Fetch tenant and plan
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError("Tenant not found.")

    plan = db.query_one(
        "SELECT p.max_users, p.name FROM tenants t JOIN plans p ON t.plan_id = p.id WHERE t.id = %s",
        (tenant_id,)
    )
    max_users = plan["max_users"] if plan else 1

    # 2. Count current active users in the tenant
    current_users_count = db.query_one(
        "SELECT COUNT(*) AS n FROM user_tenant_memberships WHERE tenant_id = %s AND is_active = true",
        (tenant_id,)
    )["n"]

    if current_users_count >= max_users:
        raise PermissionError("Upgrade plan to add more team members.")

    # 3. Check if user already exists
    user = get_user_by_email(email)
    if user:
        # Check if already a member of this tenant
        membership = db.query_one(
            "SELECT * FROM user_tenant_memberships WHERE user_id = %s AND tenant_id = %s",
            (user["id"], tenant_id)
        )
        if membership and membership["is_active"]:
            raise ValueError("User is already a member of this team.")
    else:
        # Create user with a placeholder password hash
        placeholder_pass = secrets.token_hex(16)
        user_id = create_user(
            tenant_id=tenant_id,
            email=email,
            password_plaintext=placeholder_pass,
            role=role,
            email_verified=False
        )
        user = get_user_by_id(user_id)

    # 4. Check for duplicate pending invitations
    pending_invite = db.query_one(
        "SELECT * FROM team_invitations WHERE tenant_id = %s AND email = %s AND used_at IS NULL AND expires_at > now()",
        (tenant_id, email)
    )
    if pending_invite:
        raise ValueError("A pending invitation already exists for this email.")

    # 5. Create team_invitations record
    token = secrets.token_urlsafe(32)
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=48)

    db.execute(
        """
        INSERT INTO team_invitations (tenant_id, email, role, token, invited_by, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (tenant_id, email, role, token, invited_by, expires_at)
    )

    # 6. Send invitation email
    send_team_invite_email(email, tenant["business_name"], token)

    return token


def accept_invitation(token: str, password: str | None = None) -> User:
    """Find invitation, check not expired, not used.
    Create or update user, set password, email_verified=true.
    Add to tenant, assign role.
    Mark invitation used.
    """
    invitation = db.query_one(
        "SELECT * FROM team_invitations WHERE token = %s AND used_at IS NULL AND expires_at > now()",
        (token,)
    )
    if not invitation:
        raise ValueError("Invalid or expired invitation token.")

    tenant_id = invitation["tenant_id"]
    email = invitation["email"]
    role = invitation["role"]

    # Retrieve or create user
    user_dict = get_user_by_email(email)
    if not user_dict:
        placeholder_pass = secrets.token_hex(16)
        user_id = create_user(
            tenant_id=tenant_id,
            email=email,
            password_plaintext=placeholder_pass,
            role=role,
            email_verified=False
        )
        user_dict = get_user_by_id(user_id)

    # Update password and set active tenant
    password_hash = user_dict["password_hash"]
    if password:
        salt = bcrypt.gensalt(rounds=12)
        password_hash = bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

    db.execute(
        "UPDATE users SET password_hash = %s, email_verified = true, tenant_id = %s, role = %s WHERE id = %s",
        (password_hash, tenant_id, role, user_dict["id"])
    )

    # Add user to memberships table
    db.execute(
        """
        INSERT INTO user_tenant_memberships (user_id, tenant_id, role, is_active)
        VALUES (%s, %s, %s, true)
        ON CONFLICT (user_id, tenant_id) DO UPDATE SET role = EXCLUDED.role, is_active = true
        """,
        (user_dict["id"], tenant_id, role)
    )

    # Mark invitation as used
    db.execute(
        "UPDATE team_invitations SET used_at = now() WHERE id = %s",
        (invitation["id"],)
    )

    # Reload updated user dict to construct User class
    updated_user = get_user_by_id(user_dict["id"])
    return User(updated_user)


def change_role(tenant_id: int, user_id: int, new_role: str, changed_by: int) -> None:
    """Change team member's role.
    tenant_admin can promote to admin, agent, viewer.
    Cannot change own role; cannot demote another admin without another admin existing.
    """
    if user_id == changed_by:
        raise ValueError("Cannot change your own role.")

    # Check target current membership
    target = db.query_one(
        "SELECT role FROM user_tenant_memberships WHERE user_id = %s AND tenant_id = %s AND is_active = true",
        (user_id, tenant_id)
    )
    if not target:
        raise ValueError("User is not a member of this tenant.")

    current_role = target["role"]

    # Demoting an admin safeguard
    if current_role == "admin" and new_role != "admin":
        admins_count = db.query_one(
            "SELECT COUNT(*) AS n FROM user_tenant_memberships WHERE tenant_id = %s AND role = 'admin' AND is_active = true",
            (tenant_id,)
        )["n"]
        if admins_count <= 1:
            raise ValueError("Cannot demote the only administrator of this tenant.")

    # Update membership role
    db.execute(
        "UPDATE user_tenant_memberships SET role = %s WHERE user_id = %s AND tenant_id = %s",
        (new_role, user_id, tenant_id)
    )

    # Update users table if this is currently the user's active tenant
    db.execute(
        "UPDATE users SET role = %s WHERE id = %s AND tenant_id = %s",
        (new_role, user_id, tenant_id)
    )

    # Log to audit_log
    db.execute(
        """
        INSERT INTO audit_log (admin_user_id, action, target_tenant_id, reason, metadata)
        VALUES (%s, 'change_role', %s, %s, %s)
        """,
        (
            changed_by,
            tenant_id,
            f"Changed role of user {user_id} to {new_role}",
            json.dumps({"target_user_id": user_id, "new_role": new_role})
        )
    )


def remove_user(tenant_id: int, user_id: int, changed_by: int) -> None:
    """Remove a user from the tenant team."""
    if user_id == changed_by:
        raise ValueError("Cannot remove yourself from the team.")

    target = db.query_one(
        "SELECT role FROM user_tenant_memberships WHERE user_id = %s AND tenant_id = %s AND is_active = true",
        (user_id, tenant_id)
    )
    if not target:
        raise ValueError("User is not a member of this tenant.")

    if target["role"] == "admin":
        admins_count = db.query_one(
            "SELECT COUNT(*) AS n FROM user_tenant_memberships WHERE tenant_id = %s AND role = 'admin' AND is_active = true",
            (tenant_id,)
        )["n"]
        if admins_count <= 1:
            raise ValueError("Cannot remove the only administrator of this tenant.")

    # Delete membership
    db.execute(
        "DELETE FROM user_tenant_memberships WHERE user_id = %s AND tenant_id = %s",
        (user_id, tenant_id)
    )

    # If this was their active tenant, clear it
    db.execute(
        "UPDATE users SET tenant_id = NULL, role = 'viewer' WHERE id = %s AND tenant_id = %s",
        (user_id, tenant_id)
    )

    # Log to audit_log
    db.execute(
        """
        INSERT INTO audit_log (admin_user_id, action, target_tenant_id, reason, metadata)
        VALUES (%s, 'remove_user', %s, %s, %s)
        """,
        (
            changed_by,
            tenant_id,
            f"Removed user {user_id} from team",
            json.dumps({"target_user_id": user_id})
        )
    )
