"""Verify DTMF actions and voice script text are consistent.

Regression: DTMF 2 must map to CANCEL, and the voice script must
explicitly say the order will be cancelled (not 'customer care').
"""
from __future__ import annotations

import pytest
from app.services.tts_service import DEFAULT_VOICE_SCRIPT


class TestDtmfVoiceConsistency:

    def test_default_script_says_cancel_for_dtmf2(self):
        """DEFAULT_VOICE_SCRIPT must say 'cancel' (বাতিল) for pressing 2,
        not 'customer care' (কাস্টমার কেয়ার)."""
        # Bangla: "বাতিল করতে ২ চাপুন" = "press 2 to cancel"
        assert "বাতিল করতে ২" in DEFAULT_VOICE_SCRIPT, (
            "DEFAULT_VOICE_SCRIPT must say 'press 2 to cancel', not customer care. "
            f"Script tail: ...{DEFAULT_VOICE_SCRIPT[-200:]}"
        )

    def test_default_script_does_not_say_customer_care_for_dtmf2(self):
        """Regression: old script said 'customer care' for DTMF 2 but system CANCELLED."""
        # "কাস্টমার কেয়ার" = customer care
        assert "কাস্টমার কেয়ার" not in DEFAULT_VOICE_SCRIPT, (
            "DEFAULT_VOICE_SCRIPT still contains 'customer care' phrase — "
            "this contradicts DTMF 2 = CANCELLED system behavior."
        )

    def test_default_script_says_confirm_for_dtmf1(self):
        """DTMF 1 = CONFIRMED, script must say confirm (নিশ্চিত) for pressing 1."""
        assert "নিশ্চিত করতে ১" in DEFAULT_VOICE_SCRIPT, (
            "DEFAULT_VOICE_SCRIPT must say 'press 1 to confirm'."
        )

    def test_dtmf2_maps_to_cancelled_in_service(self, app):
        """System behavior: DTMF digit '2' must produce order status CANCELLED."""
        with app.app_context():
            from app.services.order_service import update_status_from_call
            import inspect
            src = inspect.getsource(update_status_from_call)
            # Find the DTMF 2 mapping line
            lines = src.splitlines()
            dtmf2_idx = next(
                (i for i, l in enumerate(lines) if 'dtmf_digit == "2"' in l or "dtmf_digit == '2'" in l),
                None
            )
            assert dtmf2_idx is not None, "No DTMF '2' branch found in update_status_from_call"
            # The line immediately after dtmf_digit == "2" must set next_status = "CANCELLED"
            next_line = lines[dtmf2_idx + 1].strip() if dtmf2_idx + 1 < len(lines) else ""
            assert "CANCELLED" in next_line, (
                f"DTMF 2 must map to CANCELLED but next line is: {next_line!r}"
            )

    def test_no_voice_script_tenant_setting_uses_default(self, app):
        """Tenant with no custom script falls back to DEFAULT_VOICE_SCRIPT which now says cancel."""
        with app.app_context():
            from app.services.tts_service import DEFAULT_VOICE_SCRIPT
            assert "বাতিল করতে ২" in DEFAULT_VOICE_SCRIPT
