"""
Safety & Policy Guardrails
--------------------------
Enforces what the agent is allowed to do.

Design decisions:
- Allowlist is URL-based: domain + path prefixes must be explicitly permitted.
- Actions are classified as safe/reversible or risky/irreversible. Risky
  actions require explicit confirmation (from config) or trigger escalation.
- PII and secrets are never logged raw — they are redacted before any
  log entry is written.
- The guardrail layer is consulted by BOTH the Discovery agent and the
  Replay engine, so the same policy applies regardless of execution path.
"""

from __future__ import annotations
import re
import logging
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configurable policy
# ---------------------------------------------------------------------------

@dataclass
class GuardrailPolicy:
    """
    Runtime policy configuration. Loaded from environment or config file.
    """
    allowed_domains: list[str]
    allowed_path_prefixes: list[str]
    block_irreversible_actions: bool = False   # If True, irreversible steps raise an error
    require_confirmation_for_irreversible: bool = True
    max_steps_per_run: int = 30
    max_run_timeout_seconds: int = 120
    pii_patterns: list[str] = None  # Regex patterns to redact from logs

    def __post_init__(self):
        if self.pii_patterns is None:
            self.pii_patterns = [
                r"\b\d{9}\b",                         # SSN (9 digits)
                r"\b\d{3}-\d{2}-\d{4}\b",             # SSN with dashes
                r"\b(?:\d[ -]?){15,16}\b",             # Credit card numbers
                r"\bpassword\s*[:=]\s*\S+",            # Password patterns
                r"\btoken\s*[:=]\s*[A-Za-z0-9_\-\.]+",  # Auth tokens
            ]


# Default policy used throughout the system
DEFAULT_POLICY = GuardrailPolicy(
    allowed_domains=["localhost:8080", "localhost:8080"],
    allowed_path_prefixes=["/search", "/member/", "/health"],
    block_irreversible_actions=False,
    require_confirmation_for_irreversible=True,
    max_steps_per_run=30,
    max_run_timeout_seconds=120,
)


# ---------------------------------------------------------------------------
# Guardrail violations
# ---------------------------------------------------------------------------

class GuardrailViolation(Exception):
    """Raised when the agent attempts something outside policy."""
    def __init__(self, message: str, action: str = "", url: str = ""):
        super().__init__(message)
        self.action = action
        self.url = url


# ---------------------------------------------------------------------------
# Core guardrail checks
# ---------------------------------------------------------------------------

class Guardrails:
    def __init__(self, policy: GuardrailPolicy = DEFAULT_POLICY):
        self.policy = policy
        self._pii_re = [re.compile(p, re.IGNORECASE) for p in (policy.pii_patterns or [])]

    def check_url(self, url: str) -> None:
        """
        Assert that a URL is within the allowed domain and path prefixes.
        Raises GuardrailViolation if not.
        """
        try:
            parsed = urlparse(url)
            host = parsed.netloc or parsed.path.split("/")[0]
        except Exception:
            raise GuardrailViolation(f"Could not parse URL: {url}", url=url)

        # Domain check
        domain_ok = any(
            host == d or host.endswith("." + d)
            for d in self.policy.allowed_domains
        )
        if not domain_ok:
            raise GuardrailViolation(
                f"URL domain '{host}' is not in the allowlist {self.policy.allowed_domains}",
                url=url,
            )

        # Path check
        path = parsed.path
        path_ok = any(path.startswith(prefix) for prefix in self.policy.allowed_path_prefixes)
        if not path_ok:
            raise GuardrailViolation(
                f"URL path '{path}' is not in allowed prefixes {self.policy.allowed_path_prefixes}",
                url=url,
            )

        logger.debug(f"[guardrails] URL check passed: {url}")

    def check_action(self, action_type: str, is_reversible: bool, step_id: str = "") -> Optional[str]:
        """
        Check whether an action is permitted.
        Returns None if OK, or a string warning message for risky-but-permitted actions.
        Raises GuardrailViolation for blocked actions.
        """
        if is_reversible:
            return None  # Always permitted

        # Irreversible action
        if self.policy.block_irreversible_actions:
            raise GuardrailViolation(
                f"Step '{step_id}' ({action_type}) is irreversible and policy blocks irreversible actions.",
                action=action_type,
            )

        if self.policy.require_confirmation_for_irreversible:
            warning = (
                f"[GUARDRAIL] Step '{step_id}' is irreversible ({action_type}). "
                "Flagged for confirmation before proceeding."
            )
            logger.warning(warning)
            return warning

        return None

    def redact(self, text: str) -> str:
        """
        Redact sensitive patterns from a string before it is logged or persisted.
        This must be called on any text that might contain PII, credentials,
        or sensitive values extracted from the UI.
        """
        if not text:
            return text
        result = text
        for pattern in self._pii_re:
            result = pattern.sub("[REDACTED]", result)
        return result

    def redact_dict(self, data: dict, sensitive_keys: list[str] = None) -> dict:
        """
        Recursively redact a dict for safe logging.
        Keys in sensitive_keys have their values replaced entirely.
        All string values are also run through the pattern redactor.
        """
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
        """
        Validate that runtime parameters match the artifact's declared schema.
        """
        for param_name, param_schema in schema.items():
            if param_name not in parameters:
                raise ValueError(
                    f"Missing required parameter '{param_name}' "
                    f"(expected type: {param_schema.get('type', 'unknown')})"
                )
            value = parameters[param_name]
            expected_type = param_schema.get("type", "string")
            if expected_type == "string" and not isinstance(value, str):
                raise ValueError(f"Parameter '{param_name}' must be a string, got {type(value).__name__}")
            if expected_type == "int" and not isinstance(value, int):
                raise ValueError(f"Parameter '{param_name}' must be an int, got {type(value).__name__}")
            if expected_type == "float" and not isinstance(value, (int, float)):
                raise ValueError(f"Parameter '{param_name}' must be a number, got {type(value).__name__}")
