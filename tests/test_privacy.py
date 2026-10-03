import pytest
from barq_ai_support.agent.privacy import mask_text, sanitize_value

def test_password_masking():
    samples = [
        ("User forgot password: Secret123!", "User forgot password: ********!"),
        ("My pwd is MyP@ssw0rd, please reset", "My pwd is ********, please reset"),
        ("Temporary password is 'TempPass2026'", "Temporary password is ********"),
    ]
    for raw, expected in samples:
        assert mask_text(raw).strip() == expected.strip()


def test_tokens_and_api_keys():
    token_sample = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    api_key_sample = "OpenAI key: sk-abcdefghijklmnopqrstuvwxyz123456"

    assert "[REDACTED_TOKEN]" in mask_text(token_sample)
    assert "[REDACTED_API_KEY]" in mask_text(api_key_sample)


def test_nested_payload_sanitization():
    payload = {
        "short_description": "User password is MyPassword99",
        "description": "Token is Bearer abcdef1234567890abcdef",
        "metadata": {
            "notes": ["password: 123456", "clean note"],
            "status": "new",
        },
    }

    cleaned = sanitize_value(payload)

    assert "MyPassword99" not in str(cleaned)
    assert "********" in cleaned["short_description"]
    assert "[REDACTED_TOKEN]" in cleaned["description"]
    assert "********" in cleaned["metadata"]["notes"][0]
    assert cleaned["metadata"]["notes"][1] == "clean note"