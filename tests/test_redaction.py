"""
test_redaction.py
=================

Tests for the PII redaction module. Run with: pytest tests/

These tests document the KNOWN AMBIGUITY in regex-based redaction:
a 12-digit number is treated as Aadhaar, a 16-digit one as a card.
There is no perfect regex for this -- the tests pin the chosen heuristic.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from observability_sdk.redaction import redact_pii


def test_email_is_redacted():
    assert "[EMAIL]" in redact_pii("reach me at a.b@example.com now")
    assert "@example.com" not in redact_pii("reach me at a.b@example.com now")


def test_credit_card_16_digits():
    assert "[CARD]" in redact_pii("pay with 4111 1111 1111 1111 please")
    assert "[CARD]" in redact_pii("pay with 4111-1111-1111-1111 please")


def test_ssn():
    assert "[SSN]" in redact_pii("ssn is 123-45-6789 ok")


def test_aadhaar_12_digits():
    assert "[AADHAAR]" in redact_pii("aadhaar 1234 5678 9012 done")


def test_card_and_aadhaar_disambiguation():
    """16-digit -> card, 12-digit -> Aadhaar. The documented heuristic."""
    assert "[CARD]" in redact_pii("1234 5678 9012 3456")
    assert "[AADHAAR]" in redact_pii("1234 5678 9012")


def test_ip_address():
    assert "[IP]" in redact_pii("host 10.0.0.255 unreachable")


def test_phone():
    assert "[PHONE]" in redact_pii("call +1 415 555 0199 today")


def test_clean_text_untouched():
    clean = "The quick brown fox jumps over the lazy dog."
    assert redact_pii(clean) == clean


def test_empty_input():
    assert redact_pii("") == ""
