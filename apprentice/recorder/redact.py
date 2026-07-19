"""Deterministic sanitizers for retained recorder artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from email import policy
from email.parser import BytesParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
BINARY_OMITTED = "[BINARY OMITTED]"

DEFAULT_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "cookie",
        "current_password",
        "new_password",
        "one_time_code",
        "otp",
        "passcode",
        "password",
        "refresh_token",
        "secret",
        "set_cookie",
        "token",
    }
)
SENSITIVE_AUTOCOMPLETE_VALUES = frozenset(
    {"current-password", "new-password", "one-time-code"}
)
REMOVED_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization", "set-cookie"})


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _sensitive_key_set(keys: Iterable[str]) -> frozenset[str]:
    return frozenset(_normalized_key(key) for key in keys)


def is_sensitive_key(
    key: str,
    *,
    sensitive_keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS,
) -> bool:
    """Return whether a field name identifies credential-like content."""

    normalized = _normalized_key(key)
    for sensitive in _sensitive_key_set(sensitive_keys):
        if normalized == sensitive:
            return True
        if normalized.startswith(f"{sensitive}_") or normalized.endswith(f"_{sensitive}"):
            return True
    return False


def is_sensitive_field(
    *,
    name: str | None = None,
    input_type: str | None = None,
    autocomplete: str | None = None,
    sensitive_keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS,
) -> bool:
    """Return whether an HTML input must be retained only as a redacted event."""

    if input_type and input_type.casefold().strip() == "password":
        return True
    autocomplete_tokens = set((autocomplete or "").casefold().split())
    if autocomplete_tokens & SENSITIVE_AUTOCOMPLETE_VALUES:
        return True
    return bool(name and is_sensitive_key(name, sensitive_keys=sensitive_keys))


def redact_text(value: str, secret_values: Iterable[str] = ()) -> str:
    """Remove configured literal sentinels from otherwise non-sensitive text."""

    result = value
    secrets = sorted({secret for secret in secret_values if secret}, key=len, reverse=True)
    for secret in secrets:
        result = result.replace(secret, REDACTED)
    return result


def sanitize_value(
    value: Any,
    *,
    sensitive_keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS,
    secret_values: Iterable[str] = (),
    key: str | None = None,
) -> Any:
    """Recursively sanitize a JSON-like value without stringifying unknown objects."""

    if key is not None and is_sensitive_key(key, sensitive_keys=sensitive_keys):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize_value(
                item,
                sensitive_keys=sensitive_keys,
                secret_values=secret_values,
                key=str(item_key),
            )
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            sanitize_value(
                item,
                sensitive_keys=sensitive_keys,
                secret_values=secret_values,
            )
            for item in value
        ]
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return BINARY_OMITTED
    if isinstance(value, str):
        return redact_text(value, secret_values)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError(f"unsupported retained artifact value: {type(value).__name__}")


def sanitize_headers(
    headers: Mapping[str, str] | Iterable[tuple[str, str]],
    *,
    secret_values: Iterable[str] = (),
) -> dict[str, str]:
    """Remove credential headers and normalize remaining names for stable output."""

    items = headers.items() if isinstance(headers, Mapping) else headers
    sanitized: dict[str, str] = {}
    for name, value in items:
        normalized = name.casefold().strip()
        if normalized in REMOVED_HEADERS:
            continue
        clean_value = redact_text(str(value), secret_values)
        if normalized == "content-type" and clean_value.casefold().startswith(
            "multipart/form-data"
        ):
            clean_value = "multipart/form-data"
        sanitized[normalized] = clean_value
    return dict(sorted(sanitized.items()))


def sanitize_url(
    url: str,
    *,
    sensitive_keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS,
    secret_values: Iterable[str] = (),
) -> str:
    """Redact sensitive query values and remove URL user information."""

    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"

    query: list[tuple[str, str]] = []
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        clean_name = redact_text(name, secret_values)
        clean_value = (
            REDACTED
            if is_sensitive_key(name, sensitive_keys=sensitive_keys)
            else redact_text(value, secret_values)
        )
        query.append((clean_name, clean_value))
    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            redact_text(parsed.path, secret_values),
            urlencode(query),
            redact_text(parsed.fragment, secret_values),
        )
    )


def _pairs_to_mapping(pairs: Iterable[tuple[str, str]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        existing = result.get(name)
        if existing is None:
            result[name] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            result[name] = [existing, value]
    return result


def _sanitize_multipart(
    raw: bytes,
    content_type: str,
    *,
    sensitive_keys: Iterable[str],
    secret_values: Iterable[str],
) -> dict[str, Any]:
    envelope = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + raw
    )
    message = BytesParser(policy=policy.default).parsebytes(envelope)
    if not message.is_multipart():
        return {"kind": "multipart", "fields": {}, "files": []}

    fields: dict[str, Any] = {}
    files: list[dict[str, str]] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition") or ""
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            files.append(
                {
                    "field": redact_text(name, secret_values),
                    "filename": redact_text(filename, secret_values),
                    "content_type": part.get_content_type(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
            continue

        charset = part.get_content_charset() or "utf-8"
        decoded = payload.decode(charset, errors="replace")
        clean = (
            REDACTED
            if is_sensitive_key(name, sensitive_keys=sensitive_keys)
            else redact_text(decoded, secret_values)
        )
        existing = fields.get(name)
        if existing is None:
            fields[name] = clean
        elif isinstance(existing, list):
            existing.append(clean)
        else:
            fields[name] = [existing, clean]
    return {
        "kind": "multipart",
        "fields": sanitize_value(
            fields,
            sensitive_keys=sensitive_keys,
            secret_values=secret_values,
        ),
        "files": files,
    }


def sanitize_body(
    body: Any,
    content_type: str | None = None,
    *,
    sensitive_keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS,
    secret_values: Iterable[str] = (),
) -> Any:
    """Parse common HTTP bodies and retain only sanitized structured content."""

    if body is None:
        return None
    if isinstance(body, (Mapping, list, tuple)):
        return sanitize_value(
            body,
            sensitive_keys=sensitive_keys,
            secret_values=secret_values,
        )

    if isinstance(body, str):
        raw = body.encode("utf-8")
    elif isinstance(body, bytes):
        raw = body
    else:
        raise TypeError(f"unsupported HTTP body: {type(body).__name__}")

    media_type = (content_type or "").partition(";")[0].casefold().strip()
    if media_type == "application/json" or media_type.endswith("+json"):
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return redact_text(raw.decode("utf-8", errors="replace"), secret_values)
        return sanitize_value(
            parsed,
            sensitive_keys=sensitive_keys,
            secret_values=secret_values,
        )
    if media_type == "application/x-www-form-urlencoded":
        pairs = parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        return sanitize_value(
            _pairs_to_mapping(pairs),
            sensitive_keys=sensitive_keys,
            secret_values=secret_values,
        )
    if media_type == "multipart/form-data":
        return _sanitize_multipart(
            raw,
            content_type or media_type,
            sensitive_keys=sensitive_keys,
            secret_values=secret_values,
        )
    if media_type.startswith("text/") or not media_type:
        return redact_text(raw.decode("utf-8", errors="replace"), secret_values)
    return BINARY_OMITTED
