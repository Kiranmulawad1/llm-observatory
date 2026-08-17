"""Alert rule logic that needs no database."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from lo_core.services.alerts import is_breached, sign_payload


class TestBreachDetection:
    @pytest.mark.parametrize(
        ("value", "comparison", "threshold", "expected"),
        [
            (0.10, "above", 0.05, True),
            (0.02, "above", 0.05, False),
            # Strictly above: exactly at the threshold is not a breach, so a
            # rule set to the current steady-state value does not fire forever.
            (0.05, "above", 0.05, False),
            (0.90, "below", 0.95, True),
            (0.99, "below", 0.95, False),
            (0.95, "below", 0.95, False),
        ],
    )
    def test_comparisons(
        self, value: float, comparison: str, threshold: float, expected: bool
    ) -> None:
        assert is_breached(value, comparison, threshold) is expected


class TestWebhookSignature:
    def test_signature_is_verifiable_by_the_receiver(self) -> None:
        """A receiver must be able to prove the alert came from us.

        Without this, anyone who learns the webhook URL can forge alerts — and
        alert endpoints routinely page a human or open a ticket.
        """
        from lo_core.config import get_settings

        payload = json.dumps({"type": "alert.triggered"}, separators=(",", ":")).encode()
        signature = sign_payload(payload)

        secret = get_settings().api_key_pepper.get_secret_value()
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(signature, expected)

    def test_any_byte_change_changes_the_signature(self) -> None:
        """Signed over exact bytes, which is why the receiver must verify the
        raw body rather than a re-serialised dict."""
        a = sign_payload(b'{"value":0.1}')
        b = sign_payload(b'{"value":0.2}')
        assert a != b

    def test_signature_is_stable(self) -> None:
        payload = b'{"stable":true}'
        assert sign_payload(payload) == sign_payload(payload)
