"""Pattern-based redaction for common secrets and personal data.

Passwords are not handled here: they are masked by the ServiceNow "before"
Business Rule (businessRule/mask_password_before.js) before the incident is
saved.
"""

import re


_SENSITIVE_PATTERNS = (
    (
        re.compile(
            r"(?i)\b(secret|token|"
            r"authorization|cookie|api[_ -]?key|access[_ -]?token|"
            r"refresh[_ -]?token|client[_ -]?secret)\b"
            r"(\s*[:=]\s*)(?:\"([^\"]*)\"|'([^']*)'|"
            r"((?:Bearer\s+)?[^\s,;]+))"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]+"), "Basic [REDACTED]"),
    (
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
        "[REDACTED_EMAIL]",
    ),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (
        re.compile(
            r"(?<!\w)(?:\+?\d{1,3}[-.\s]?)?"
            r"(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}(?!\w)"
        ),
        "[REDACTED_PHONE]",
    ),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "[REDACTED_NUMBER]"),
)


def mask_sensitive_text(text: str) -> str:
    """Redact common credential and personal-data formats from text."""

    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text