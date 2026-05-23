"""
redaction.py
============

PII redaction. Deliberately simple: regex-based detection of the most
common PII shapes. This is a BEST-EFFORT scrub, not a compliance tool.

Why regex and not an ML model?
    * Predictable, fast, zero dependencies, easy to explain.
    * The tradeoff: it catches well-formed patterns (emails, cards, phones)
      but will miss free-form PII like names or addresses. That limitation
      is documented in the README under "what I'd improve".

ORDER MATTERS. Specific patterns (SSN, Aadhaar, credit card) must run
BEFORE the loose phone pattern, otherwise the phone regex -- which matches
many digit groupings -- swallows them first and mislabels them as [PHONE].
This was a real bug caught by the test suite; the ordering below is the fix.

Where it runs:
    * In the SDK, before previews are sent over the wire (defense in depth).
    * Again in the ingestion service, so redaction is enforced even if a
      log arrives from an older/untrusted SDK. Server-side is the real
      guarantee; client-side is just early reduction of exposure.
"""
import re

# Patterns are applied in THIS ORDER. Specific shapes first.
_PATTERNS = [
    # Email addresses
    (re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"), "[EMAIL]"),
    # US SSN -- must run before phone.
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    # Credit-card-like: 16 digits in 4 groups of 4. A 12-digit run is
    # NOT matched here -- that is left for the Aadhaar pattern. This is
    # a documented heuristic: 16 digits => card, 12 digits => Aadhaar.
    (re.compile(r"\b\d{4}(?:[ -]\d{4}){3}\b"), "[CARD]"),
    # India Aadhaar -- EXACTLY 12 digits as 4-4-4 (3 groups), space-
    # separated, and not part of a longer run.
    (re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b(?![ -]?\d)"), "[AADHAAR]"),
    # IPv4 addresses
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    # Phone numbers -- loosest pattern, runs LAST so specific ones win.
    (re.compile(r"\b(?:\+?\d{1,3}[ -]?)?\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}\b"),
     "[PHONE]"),
]


def redact_pii(text: str) -> str:
    """Return a copy of `text` with detected PII replaced by placeholders.

    Patterns are applied in list order; see the module docstring for why
    that order is load-bearing.
    """
    if not text:
        return text
    redacted = text
    for pattern, placeholder in _PATTERNS:
        redacted = pattern.sub(placeholder, redacted)
    return redacted
