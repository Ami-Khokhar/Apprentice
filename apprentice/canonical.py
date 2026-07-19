from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import BaseModel


def _json_value(value: Any) -> Any:
    """Return a strict JSON value without silently stringifying domain objects."""
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return _json_value(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Canonical JSON does not support NaN or infinity")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Canonical JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Encode a JSON-compatible value deterministically as compact UTF-8."""
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: Any) -> str:
    """Return the SHA-256 digest of a value's canonical JSON encoding."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


class InvalidBaseUrlError(ValueError):
    pass


_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def normalize_hostname(value: str) -> str:
    candidate = value.lower().rstrip(".")
    if not candidate or "%" in candidate:
        raise InvalidBaseUrlError("Target base URL has an invalid hostname")
    try:
        return ipaddress.ip_address(candidate).compressed.lower()
    except ValueError:
        pass
    if ":" in candidate or all(character in "0123456789." for character in candidate):
        raise InvalidBaseUrlError("Target base URL has an invalid hostname")
    try:
        ascii_hostname = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise InvalidBaseUrlError("Target base URL has an invalid hostname") from error
    if len(ascii_hostname) > 253:
        raise InvalidBaseUrlError("Target base URL has an invalid hostname")
    labels = ascii_hostname.split(".")
    if any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise InvalidBaseUrlError("Target base URL has an invalid hostname")
    return ascii_hostname


def normalize_base_url(value: str) -> tuple[str, str]:
    """Normalize an approved HTTP(S) base URL and return its canonical host."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise InvalidBaseUrlError(f"Invalid target base URL: {value}") from error
    if parsed.scheme.lower() not in {"http", "https"} or parsed.hostname is None:
        raise InvalidBaseUrlError("Target base URL must use http or https and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidBaseUrlError("Target base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise InvalidBaseUrlError("Target base URL must not contain a query or fragment")
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.endswith(":"):
        raise InvalidBaseUrlError("Target base URL has a malformed port")

    scheme = parsed.scheme.lower()
    hostname = normalize_hostname(parsed.hostname)
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    port_suffix = "" if port is None or default_port else f":{port}"
    netloc = f"[{hostname}]{port_suffix}" if ":" in hostname else f"{hostname}{port_suffix}"
    path = parsed.path.rstrip("/")
    return urlunsplit(SplitResult(scheme, netloc, path, "", "")), hostname
