"""文件作用：敏感信息识别和日志脱敏。

项目关系：本文件依赖 无直接本地模块依赖；被 `agent.query_agent`、`bot.feishu_bot`、`core.llm_client`、`core.observability` 等 17 个模块。
"""



from __future__ import annotations

import re
from dataclasses import dataclass


HIGH_RISK_IDENTIFIER_LABELS = (
    "身份证", "身份证号", "身份号码", "证件号", "证件号码",
    "银行卡", "银行卡号", "银行账号", "社保号", "护照号",
)

SENSITIVE_TOPIC_KEYWORDS = (
    "密码",
    "口令",
    "私钥",
    "密钥",
    *HIGH_RISK_IDENTIFIER_LABELS,
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
_URL_CREDENTIAL_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|key|token|secret|access[_-]?token|access[_-]?key|ticket)=)([^&\s]+)"
)
_IDENTIFIER_RE = re.compile(
    r"(?:身份证(?:号)?|银行卡(?:号)?|卡号|银行账号|账号)\s*(?:是|为|[:：=])?\s*"
    r"(?:\d[ -]?){12,19}[0-9Xx]"
)


@dataclass(frozen=True)
class SensitiveAssessment:
    """类功能：`SensitiveAssessment` 封装与“敏感信息识别和日志脱敏”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    sensitive: bool
    category: str | None = None
    reason: str | None = None
    blocks_storage: bool = False


def assess_sensitive_text(text: str) -> SensitiveAssessment:
    """函数功能：`assess_sensitive_text` 负责处理 assess sensitive text，服务于本文件职责：敏感信息识别和日志脱敏。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `SensitiveAssessment` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
    compact = value.casefold()
    if any(label.casefold() in compact for label in HIGH_RISK_IDENTIFIER_LABELS):
        return SensitiveAssessment(True, "identifier", "high_risk_identifier_label", True)
    return SensitiveAssessment(False)


def contains_sensitive_data(text: str) -> bool:
    """函数功能：`contains_sensitive_data` 负责处理 contains sensitive data，服务于本文件职责：敏感信息识别和日志脱敏。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return assess_sensitive_text(text).sensitive


def mentions_sensitive_topic(text: str) -> bool:
    """函数功能：`mentions_sensitive_topic` 负责处理 mentions sensitive topic，服务于本文件职责：敏感信息识别和日志脱敏。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    compact = str(text or "").casefold()
    return any(keyword in compact for keyword in SENSITIVE_TOPIC_KEYWORDS)


def redact_sensitive_text(text: str) -> str:
    """函数功能：`redact_sensitive_text` 负责脱敏 sensitive text，服务于本文件职责：敏感信息识别和日志脱敏。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
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
    """函数功能：`safe_text_preview` 负责处理 safe text preview，服务于本文件职责：敏感信息识别和日志脱敏。
    传参：
        text: 输入文本内容，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `80`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    if assess_sensitive_text(text).blocks_storage:
        return "[sensitive content redacted]"
    return redact_sensitive_text(str(text or "").replace("\n", " "))[:limit]
