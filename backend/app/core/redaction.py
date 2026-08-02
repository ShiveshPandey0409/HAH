from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "bearer",
        "clientsecret",
        "cookie",
        "credential",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "sessiontoken",
        "signingkey",
        "token",
    }
)
_KNOWN_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "bearer",
        "clientsecret",
        "cookie",
        "credential",
        "password",
        "privatekey",
        "providersecret",
        "refreshtoken",
        "secret",
        "sessiontoken",
        "signingkey",
        "token",
    }
)
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)(?<![a-z0-9])(?:access[_-]?token|api[_-]?key|authorization|"
        r"client[_-]?secret|cookie|credential|password|private[_-]?key|"
        r"refresh[_-]?token|secret|session[_-]?token|signing[_-]?key|token)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(
        r"(?<![A-Za-z0-9_-])hah\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
        r"(?![A-Za-z0-9_-])"
    ),
    re.compile(r"(?<![A-Za-z0-9_-])whsec_[A-Za-z0-9_-]+(?![A-Za-z0-9_-])"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _contains_sensitive_value(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SENSITIVE_VALUE_PATTERNS)


def _safe_mapping_key(
    existing: dict[str, Any],
    key: object,
    *,
    contains_sensitive_name: bool,
    normalized_key: str,
) -> str:
    key_text = str(key)
    contains_sensitive_value = _contains_sensitive_value(key_text)
    contains_embedded_sensitive_value = (
        contains_sensitive_name and normalized_key not in _KNOWN_SENSITIVE_FIELD_NAMES
    )
    if not contains_sensitive_value and not contains_embedded_sensitive_value:
        return key_text

    candidate = "[REDACTED_KEY]"
    suffix = 2
    while candidate in existing:
        candidate = f"[REDACTED_KEY_{suffix}]"
        suffix += 1
    return candidate


def redact_sensitive_data(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            contains_sensitive_name = any(part in normalized_key for part in _SENSITIVE_KEY_PARTS)
            safe_key = _safe_mapping_key(
                redacted,
                key,
                contains_sensitive_name=contains_sensitive_name,
                normalized_key=normalized_key,
            )
            if contains_sensitive_name:
                redacted[safe_key] = REDACTED
            else:
                redacted[safe_key] = redact_sensitive_data(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, str) and _contains_sensitive_value(value):
        return REDACTED
    return value


def canonical_json_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
