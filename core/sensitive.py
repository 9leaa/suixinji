"""Local high-confidence sensitive-content detection and redaction.

This module must stay deterministic and network-free because it runs before
WAL persistence, classification, embeddings, or any other external request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


SENSITIVE_TOPIC_KEYWORDS = (
    "密码",
    "口令",
    "私钥",
    "密钥",
    "身份证",
    "银行卡",
    "api key",
    "apikey",
    "access key",
    "token",
    "secret",
    "password",
    "authorization",
)

_CREDENTIAL_LABEL_VALUE_RE = re.compile(
    r"(?i)(密码|口令|私钥|密钥|api[_ -]?key|access[_ -]?key|token|secret|password)"
    r"\s*(?:是|为|[:：=])\s*([^\s，。；;]{4,})"
)
_CREDENTIAL_SPACE_VALUE_RE = re.compile(
    r"(?i)(密码|口令|私钥|密钥|api[_ -]?key|access[_ -]?key|token|secret|password)"
    r"\s+(?=[^\s，。；;]{8,})(?=[^\s，。；;]*\d)([^\s，。；;]{8,})"
)
_PREFIXED_SECRET_RE = re.compile(r"(?i)\b(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}\b")
_PROVIDER_SECRET_RE = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,})"
)
_JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.DOTALL,
)
_CONNECTION_CREDENTIAL_RE = re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s:/]+:[^\s@]+@")
_URL_CREDENTIAL_RE = re.compile(r"(?i)([?&](?:access_key|ticket)=)([^&\s]+)")
_IDENTIFIER_RE = re.compile(
    r"(?:身份证(?:号)?|银行卡(?:号)?|卡号|银行账号|账号)\s*(?:是|为|[:：=])?\s*"
    r"(?:\d[ -]?){12,19}[0-9Xx]"
)


@dataclass(frozen=True)
class SensitiveAssessment:
    sensitive: bool
    category: str | None = None
    reason: str | None = None
    blocks_storage: bool = False


def assess_sensitive_text(text: str) -> SensitiveAssessment:
    value = str(text or "")
    if _PRIVATE_KEY_RE.search(value):
        return SensitiveAssessment(True, "private_key", "private_key_block", True)
    if _BEARER_RE.search(value):
        return SensitiveAssessment(True, "credential", "bearer_token", True)
    if _CONNECTION_CREDENTIAL_RE.search(value):
        return SensitiveAssessment(True, "credential", "connection_string_credentials", True)
    if _URL_CREDENTIAL_RE.search(value):
        return SensitiveAssessment(True, "credential", "url_credentials", True)
    if _PROVIDER_SECRET_RE.search(value):
        return SensitiveAssessment(True, "credential", "provider_secret", True)
    if _JWT_RE.search(value):
        return SensitiveAssessment(True, "credential", "jwt", True)
    if _PREFIXED_SECRET_RE.search(value):
        return SensitiveAssessment(True, "credential", "prefixed_secret", True)
    if _CREDENTIAL_LABEL_VALUE_RE.search(value):
        return SensitiveAssessment(True, "credential", "credential_label_with_value", True)
    if _CREDENTIAL_SPACE_VALUE_RE.search(value):
        return SensitiveAssessment(True, "credential", "credential_label_with_space_value", True)
    if _IDENTIFIER_RE.search(value):
        return SensitiveAssessment(True, "identifier", "high_risk_identifier", True)
    return SensitiveAssessment(False)


def contains_sensitive_data(text: str) -> bool:
    return assess_sensitive_text(text).sensitive


def mentions_sensitive_topic(text: str) -> bool:
    compact = str(text or "").casefold()
    return any(keyword in compact for keyword in SENSITIVE_TOPIC_KEYWORDS)


def redact_sensitive_text(text: str) -> str:
    value = str(text or "")
    value = _PRIVATE_KEY_RE.sub("[PRIVATE_KEY_REDACTED]", value)
    value = _BEARER_RE.sub("Bearer [REDACTED]", value)
    value = _CONNECTION_CREDENTIAL_RE.sub(lambda match: match.group(0).split("://", 1)[0] + "://[REDACTED]@", value)
    value = _URL_CREDENTIAL_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    value = _PROVIDER_SECRET_RE.sub("[SECRET_REDACTED]", value)
    value = _JWT_RE.sub("[TOKEN_REDACTED]", value)
    value = _PREFIXED_SECRET_RE.sub("[SECRET_REDACTED]", value)
    value = _CREDENTIAL_LABEL_VALUE_RE.sub(lambda match: f"{match.group(1)}：[REDACTED]", value)
    value = _CREDENTIAL_SPACE_VALUE_RE.sub(lambda match: f"{match.group(1)} [REDACTED]", value)
    value = _IDENTIFIER_RE.sub("[IDENTIFIER_REDACTED]", value)
    return value


def safe_text_preview(text: str, limit: int = 80) -> str:
    if assess_sensitive_text(text).blocks_storage:
        return "[sensitive content redacted]"
    return redact_sensitive_text(str(text or "").replace("\n", " "))[:limit]
