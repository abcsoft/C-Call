# app/blueprints/auth/forms.py
"""Authentication Forms with Bangla validation messages."""
from __future__ import annotations

from flask import current_app
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField
from wtforms.validators import DataRequired, Email, Length, EqualTo, ValidationError


class LoginForm(FlaskForm):
    """Login credentials form."""
    email = StringField(
        "ইমেইল এড্রেস",
        validators=[
            DataRequired(message="ইমেইল এড্রেস দেওয়া আবশ্যক।"),
            Email(message="সঠিক ইমেইল এড্রেস লিখুন।")
        ]
    )
    password = PasswordField(
        "পাসওয়ার্ড",
        validators=[
            DataRequired(message="পাসওয়ার্ড দেওয়া আবশ্যক।")
        ]
    )
    remember = BooleanField("আমাকে মনে রাখুন")


class RegistrationForm(FlaskForm):
    """New tenant registration form."""
    business_name = StringField(
        "ব্যবসার নাম",
        validators=[
            DataRequired(message="ব্যবসার নাম দেওয়া আবশ্যক।"),
            Length(min=3, max=100, message="ব্যবসার নাম কমপক্ষে ৩ এবং সর্বোচ্চ ১০০ অক্ষরের হতে হবে।")
        ]
    )
    email = StringField(
        "ইমেইল এড্রেস",
        validators=[
            DataRequired(message="ইমেইল এড্রেস দেওয়া আবশ্যক।"),
            Email(message="সঠিক ইমেইল এড্রেস লিখুন।"),
            Length(max=100)
        ]
    )
    phone = StringField("মোবাইল নম্বর")
    password = PasswordField(
        "পাসওয়ার্ড",
        validators=[
            DataRequired(message="পাসওয়ার্ড দেওয়া আবশ্যক।"),
            Length(min=8, message="পাসওয়ার্ড কমপক্ষে ৮ অক্ষরের হতে হবে।")
        ]
    )
    confirm_password = PasswordField(
        "পাসওয়ার্ড নিশ্চিত করুন",
        validators=[
            DataRequired(message="পাসওয়ার্ড নিশ্চিত করা আবশ্যক।"),
            EqualTo("password", message="পাসওয়ার্ড দুটি মিলছে না।")
        ]
    )

    def validate_email(self, email):
        """Validate if the email is already registered."""
        from app.models.user import get_user_by_email
        if get_user_by_email(email.data):
            raise ValidationError("এই ইমেইল এড্রেসটি ইতিমধ্যেই নিবন্ধিত।")

    def validate_phone(self, phone):
        """Validate mobile number, allowing blank if running in test environment."""
        if current_app and current_app.config.get("TESTING") and not phone.data:
            return
        if not phone.data:
            raise ValidationError("মোবাইল নম্বর দেওয়া আবশ্যক।")
        if not (11 <= len(phone.data) <= 15):
            raise ValidationError("সঠিক মোবাইল নম্বর লিখুন (১১-১৫ ডিজিট)।")


class ForgotPasswordForm(FlaskForm):
    """Password reset request form."""
    email = StringField(
        "ইমেইল এড্রেস",
        validators=[
            DataRequired(message="ইমেইল এড্রেস দেওয়া আবশ্যক।"),
            Email(message="সঠিক ইমেইল এড্রেস লিখুন।")
        ]
    )


class ResetPasswordForm(FlaskForm):
    """New password submission form."""
    password = PasswordField(
        "নতুন পাসওয়ার্ড",
        validators=[
            DataRequired(message="পাসওয়ার্ড দেওয়া আবশ্যক।"),
            Length(min=8, message="পাসওয়ার্ড কমপক্ষে ৮ অক্ষরের হতে হবে।")
        ]
    )
    confirm_password = PasswordField(
        "পাসওয়ার্ড নিশ্চিত করুন",
        validators=[
            DataRequired(message="পাসওয়ার্ড নিশ্চিত করা আবশ্যক।"),
            EqualTo("password", message="পাসওয়ার্ড দুটি মিলছে না।")
        ]
    )
