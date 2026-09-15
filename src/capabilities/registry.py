"""
capabilities/registry.py
-------------------------
Maps a natural-language intent to the right CapabilityArtifact.

The registry is a list of (keywords, builder_fn, param_extractor_fn).
The intent router in the chat UI calls `match(intent_text)` to find
the best capability and extract the parameters it needs.
"""

from __future__ import annotations
import re
from typing import Optional, Callable

from artifact import CapabilityArtifact
import capabilities.check_balance as check_balance
import capabilities.update_status as update_status

TARGET_URL = "http://localhost:8080"


# ── Member ID extraction helper ───────────────────────────────────────────

def _extract_member_id(text: str) -> Optional[str]:
    """Pull a 6-digit member ID from free text."""
    m = re.search(r"\b(\d{6})\b", text)
    return m.group(1) if m else None


def _extract_name_to_id(text: str) -> Optional[str]:
    """Map known member names to IDs (case-insensitive)."""
    NAME_MAP = {
        "alice":   "100001",
        "alice johnson": "100001",
        "bob":     "100002",
        "bob martinez": "100002",
        "carol":   "100003",
        "carol williams": "100003",
        "david":   "100004",
        "david lee": "100004",
        "eva":     "100005",
        "eva chen": "100005",
        "test user": "999999",
    }
    lower = text.lower()
    for name, mid in NAME_MAP.items():
        if name in lower:
            return mid
    return None


def _find_member_id(text: str) -> Optional[str]:
    return _extract_member_id(text) or _extract_name_to_id(text)


def _extract_new_status(text: str) -> Optional[str]:
    lower = text.lower()
    if "frozen" in lower or "freeze" in lower:
        return "Frozen"
    if "closed" in lower or "close" in lower:
        return "Closed"
    if "active" in lower or "activate" in lower or "reactivate" in lower:
        return "Active"
    return None


def _extract_reason(text: str) -> str:
    m = re.search(r"reason[:\s]+([^\.,]+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else "operator-request"


# ── Registry entries ──────────────────────────────────────────────────────

class CapabilityMatch:
    def __init__(
        self,
        name: str,
        artifact: CapabilityArtifact,
        parameters: dict,
        missing: list[str],
    ):
        self.name       = name
        self.artifact   = artifact
        self.parameters = parameters
        self.missing    = missing   # parameter names not yet resolved

    @property
    def ready(self) -> bool:
        return len(self.missing) == 0


_REGISTRY = [
    # ── Balance / status lookup ────────────────────────────────────────────
    {
        "name": "check_member_balance",
        # Generic read-intent words — low weight so update words beat them
        "keywords": [
            "balance", "status", "look up", "lookup",
            "check", "what is", "show me", "find", "get", "view",
        ],
        # Strong exclusive triggers for this capability (weight ×3)
        "strong_keywords": ["balance"],
        "builder": lambda: check_balance.build(TARGET_URL),
        "extract_params": lambda text: {
            "member_id": _find_member_id(text),
        },
        "required": ["member_id"],
    },
    # ── Account status update ──────────────────────────────────────────────
    {
        "name": "update_account_status",
        "keywords": [
            "update", "change", "set", "activate", "reactivate",
        ],
        # These unambiguously mean "mutate the account" — weight ×5
        "strong_keywords": [
            "freeze", "frozen", "close", "closed", "deactivate",
        ],
        "builder": lambda: update_status.build(TARGET_URL),
        "extract_params": lambda text: {
            "member_id":   _find_member_id(text),
            "new_status":  _extract_new_status(text),
            "reason_code": _extract_reason(text),
        },
        "required": ["member_id", "new_status"],
    },
]


def match(user_text: str) -> Optional[CapabilityMatch]:
    """
    Find the best capability for the user's text.
    Strong keywords are weighted 5×; regular keywords 1×.
    Returns a CapabilityMatch (which may have missing params), or None.
    """
    lower = user_text.lower()
    best  = None
    best_score = 0

    for entry in _REGISTRY:
        score  = sum(1 for kw in entry.get("keywords", [])        if kw in lower)
        score += sum(5 for kw in entry.get("strong_keywords", []) if kw in lower)
        if score > best_score:
            best_score = score
            best = entry

    if not best or best_score == 0:
        return None

    params  = best["extract_params"](user_text)
    missing = [k for k in best["required"] if not params.get(k)]

    return CapabilityMatch(
        name=best["name"],
        artifact=best["builder"](),
        parameters={k: v for k, v in params.items() if v is not None},
        missing=missing,
    )


def list_capabilities() -> list[dict]:
    """Return human-readable summaries of all registered capabilities."""
    return [
        {
            "name": e["name"],
            "triggers": e["keywords"][:5],
        }
        for e in _REGISTRY
    ]
