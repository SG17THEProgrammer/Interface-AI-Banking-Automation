"""
guardrails.py — Safety & Policy Guardrails
------------------------------------------
URL allowlist, action safety classification, PII redaction.
Consulted by both the Replay engine and the Discovery agent.
"""

from __future__ import annotations
import re
import logging
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class GuardrailPolicy:
    allowed_domains: list[str]
    allowed_path_prefixes: list[str]
    block_irreversible_actions: bool = False
    require_confirmation_for_irreversible: bool = True
    max_steps_per_run: int = 30
    max_run_timeout_seconds: int = 120
    pii_patterns: list[str] = None
    pii_fields: list[str] = None

    def __post_init__(self):
        if self.pii_patterns is None:
            self.pii_patterns = [
                r"\b\d{9}\b",
                r"\b\d{3}-\d{2}-\d{4}\b",
                r"\b(?:\d[ -]?){15,16}\b",
                r"\bpassword\s*[:=]\s*\S+",
                r"\btoken\s*[:=]\s*[A-Za-z0-9_\-\.]+",
            ]
        if self.pii_fields is None:
            self.pii_fields = ["member_name"]


DEFAULT_POLICY = GuardrailPolicy(
    allowed_domains=["localhost:8080"],
    # /member/ covers /member/100001, /member/100001/update, /member/100001/confirm-update
    allowed_path_prefixes=["/search", "/member/", "/health", "/api/"],
    block_irreversible_actions=False,
    require_confirmation_for_irreversible=True,
)


class GuardrailViolation(Exception):
    def __init__(self, message: str, action: str = "", url: str = ""):
        super().__init__(message)
        self.action = action
        self.url    = url


class Guardrails:
    def __init__(self, policy: GuardrailPolicy = DEFAULT_POLICY):
        self.policy = policy
        self._pii_re = [re.compile(p, re.IGNORECASE) for p in (policy.pii_patterns or [])]

    def check_url(self, url: str) -> None:
        try:
            parsed = urlparse(url)
            host   = parsed.netloc or parsed.path.split("/")[0]
        except Exception:
            raise GuardrailViolation(f"Could not parse URL: {url}", url=url)

        if not any(host == d or host.endswith("." + d) for d in self.policy.allowed_domains):
            raise GuardrailViolation(
                f"Domain '{host}' not in allowlist {self.policy.allowed_domains}", url=url
            )

        path = urlparse(url).path
        if not any(path.startswith(p) for p in self.policy.allowed_path_prefixes):
            raise GuardrailViolation(
                f"Path '{path}' not in allowed prefixes {self.policy.allowed_path_prefixes}", url=url
            )
        logger.debug(f"[guardrails] URL OK: {url}")

    def check_action(self, action_type: str, is_reversible: bool, step_id: str = "") -> Optional[str]:
        if is_reversible:
            return None
        if self.policy.block_irreversible_actions:
            raise GuardrailViolation(
                f"Step '{step_id}' is irreversible and policy blocks such actions.",
                action=action_type,
            )
        if self.policy.require_confirmation_for_irreversible:
            warning = f"[GUARDRAIL] Step '{step_id}' is irreversible — flagged."
            logger.warning(warning)
            return warning
        return None

    def redact(self, text: str) -> str:
        if not text:
            return text
        result = text
        for pattern in self._pii_re:
            result = pattern.sub("[REDACTED]", result)
        return result

    def redact_dict(self, data: dict, sensitive_keys: list[str] = None) -> dict:
        sensitive_keys = sensitive_keys or ["password", "token", "secret", "credential", "ssn"]
        result = {}
        for k, v in data.items():
            if any(sk in k.lower() for sk in sensitive_keys):
                result[k] = "[REDACTED]"
            elif isinstance(v, str):
                result[k] = self.redact(v)
            elif isinstance(v, dict):
                result[k] = self.redact_dict(v, sensitive_keys)
            elif isinstance(v, list):
                result[k] = [
                    self.redact_dict(i, sensitive_keys) if isinstance(i, dict)
                    else (self.redact(i) if isinstance(i, str) else i)
                    for i in v
                ]
            else:
                result[k] = v
        return result

    def validate_parameters(self, parameters: dict, schema: dict) -> None:
        for name, spec in schema.items():
            if name not in parameters:
                raise ValueError(f"Missing required parameter '{name}' (type: {spec.get('type','?')})")
            val = parameters[name]
            t   = spec.get("type", "string")
            if t == "string"  and not isinstance(val, str):
                raise ValueError(f"Parameter '{name}' must be a string")
            if t == "int"     and not isinstance(val, int):
                raise ValueError(f"Parameter '{name}' must be an int")
            if t == "float"   and not isinstance(val, (int, float)):
                raise ValueError(f"Parameter '{name}' must be a number")
