# app/models/user.py
"""User model and authentication helpers."""
from __future__ import annotations

from typing import Any
import datetime
import bcrypt
from flask_login import UserMixin
from app.db import db
from app.models.tenant import get_tenant_by_id


class User(UserMixin):
    """User representation matching Flask-Login requirements."""

    def __init__(self, user_dict: dict[str, Any]) -> None:
        self.id = int(user_dict["id"])
        self.tenant_id = int(user_dict["tenant_id"]) if user_dict.get("tenant_id") is not None else None
        self.email = user_dict["email"]
        self.role = user_dict["role"]
        self.email_verified = bool(user_dict["email_verified"])

    @property
    def is_active(self) -> bool:
        if self.role == "super_admin":
            return True
        if not self.tenant_id:
            return False
        tenant = get_tenant_by_id(self.tenant_id)
        return tenant is not None and tenant["status"] in ("active", "trial")

    @property
    def is_super_admin(self) -> bool:
        return self.role == "super_admin"

    @property
    def is_tenant_admin(self) -> bool:
        return self.role == "admin"

    def get_id(self) -> str:
        return str(self.id)


class UserLoader:
    """User loader container for Flask-Login integration."""

    @staticmethod
    def load_user(user_id: str) -> User | None:
        """Loads a user by primary key string."""
        user_dict = get_user_by_id(int(user_id))
        return User(user_dict) if user_dict else None


def create_user(
    tenant_id: int | None | dict[str, Any],
    email: str | None = None,
    password_plaintext: str | None = None,
    role: str = "admin",
    email_verified: bool = False
) -> int:
    """Hash password and insert a new user login account.
    Supports both keyword arguments and a single dictionary parameter.
    """
    if isinstance(tenant_id, dict):
        email = tenant_id["email"]
        password_plaintext = tenant_id["password"]
        role = tenant_id.get("role", "admin")
        email_verified = tenant_id.get("email_verified", False)
        tenant_id = tenant_id.get("tenant_id")

    # Hash password using bcrypt cost 12
    salt = bcrypt.gensalt(rounds=12)
    password_hash = bcrypt.hashpw(password_plaintext.encode("utf-8"), salt).decode("utf-8")
    
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO users (tenant_id, email, password_hash, role, email_verified)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (tenant_id, email, password_hash, role, email_verified)
        )
        user_id = cursor.fetchone()["id"]
        
        if tenant_id is not None:
            conn.execute(
                """
                INSERT INTO user_tenant_memberships (user_id, tenant_id, role, is_active)
                VALUES (%s, %s, %s, true)
                ON CONFLICT (user_id, tenant_id) DO NOTHING
                """,
                (user_id, tenant_id, role)
            )
            
    return user_id


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    """Retrieve user details by ID."""
    return db.query_one("SELECT * FROM users WHERE id = %s", (user_id,))


def get_user_by_email(email: str) -> dict[str, Any] | None:
    """Retrieve user details by email address."""
    return db.query_one("SELECT * FROM users WHERE email = %s", (email,))


def verify_user_password(password_hash: str, password_plaintext: str) -> bool:
    """Verify raw plaintext password against hashed bcrypt password."""
    try:
        return bcrypt.checkpw(password_plaintext.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def update_password(user_id: int, password_hash: str) -> None:
    """Update a user's hashed password."""
    db.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))


def update_last_login(user_id: int) -> None:
    """Update user's last login timestamp."""
    db.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_id,))


def set_email_verified(user_id: int) -> None:
    """Verify user's email address."""
    db.execute("UPDATE users SET email_verified = true WHERE id = %s", (user_id,))


def get_users_by_tenant(tenant_id: int) -> list[dict[str, Any]]:
    """Retrieve all users associated with a tenant."""
    return db.query(
        "SELECT id, tenant_id, email, role, email_verified, last_login_at, created_at FROM users WHERE tenant_id = %s ORDER BY id",
        (tenant_id,)
    )


# --- Email Verifications / Password Reset Tokens ---

def create_verification_token(user_id: int, token: str, expires_at: datetime.datetime) -> int:
    """Insert a verification or password reset token."""
    return db.insert_returning_id(
        """
        INSERT INTO email_verifications (user_id, token, expires_at)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (user_id, token, expires_at)
    )


def get_verification_token(token: str) -> dict[str, Any] | None:
    """Retrieve details for a verification token."""
    return db.query_one("SELECT * FROM email_verifications WHERE token = %s", (token,))


def mark_token_as_used(token: str) -> None:
    """Mark a verification token as consumed."""
    db.execute("UPDATE email_verifications SET used_at = now() WHERE token = %s", (token,))
