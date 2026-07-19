from apprentice.recorder.redact import (
    REDACTED,
    is_sensitive_field,
    sanitize_body,
    sanitize_headers,
    sanitize_url,
)


def test_password_and_one_time_code_fields_are_sensitive() -> None:
    assert is_sensitive_field(input_type="password")
    assert is_sensitive_field(name="login", autocomplete="one-time-code")
    assert is_sensitive_field(name="user[password]")
    assert not is_sensitive_field(name="merchant", autocomplete="organization")


def test_headers_query_and_structured_bodies_remove_secrets() -> None:
    headers = sanitize_headers(
        {
            "Authorization": "Bearer hunter2",
            "Cookie": "session=123456",
            "Set-Cookie": "session=hunter2",
            "X-Request-Id": "stable",
        },
        secret_values=("hunter2", "123456"),
    )
    assert headers == {"x-request-id": "stable"}

    url = sanitize_url(
        "https://user:hunter2@example.test/path?token=hunter2&merchant=123456",
        secret_values=("hunter2", "123456"),
    )
    assert url == "https://example.test/path?token=%5BREDACTED%5D&merchant=%5BREDACTED%5D"

    body = sanitize_body(
        '{"password":"hunter2","nested":{"otp":"123456"},"merchant":"Acme"}',
        "application/json",
        secret_values=("hunter2", "123456"),
    )
    assert body == {
        "password": REDACTED,
        "nested": {"otp": REDACTED},
        "merchant": "Acme",
    }


def test_form_and_multipart_bodies_are_parsed_before_redaction() -> None:
    form = sanitize_body(
        "merchant=Acme&password=hunter2&otp=123456",
        "application/x-www-form-urlencoded",
        secret_values=("hunter2", "123456"),
    )
    assert form == {"merchant": "Acme", "password": REDACTED, "otp": REDACTED}

    boundary = "fixture-boundary"
    multipart = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="merchant"\r\n\r\n'
        "Acme\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="password"\r\n\r\n'
        "hunter2\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="receipt"; filename="receipt.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        "safe-file\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    sanitized = sanitize_body(
        multipart,
        f"multipart/form-data; boundary={boundary}",
        secret_values=("hunter2",),
    )
    assert sanitized["fields"] == {"merchant": "Acme", "password": REDACTED}
    assert sanitized["files"][0]["filename"] == "receipt.pdf"
    assert "safe-file" not in str(sanitized)
