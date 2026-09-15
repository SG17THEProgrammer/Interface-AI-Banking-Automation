"""
In-memory database for the mock banking app.
Kept in a module-level dict so mutations persist across requests
within a single server process.
"""

from __future__ import annotations
import copy
from datetime import datetime, timezone
from typing import Optional

# ── Seed data ─────────────────────────────────────────────────────────────

_SEED: dict[str, dict] = {
    "100001": {
        "name": "Alice Johnson",
        "balance": 4521.00,
        "account_status": "Active",
        "account_type": "Savings",
    },
    "100002": {
        "name": "Bob Martinez",
        "balance": 12340.50,
        "account_status": "Active",
        "account_type": "Checking",
    },
    "100003": {
        "name": "Carol Williams",
        "balance": 750.25,
        "account_status": "Frozen",
        "account_type": "Savings",
    },
    "100004": {
        "name": "David Lee",
        "balance": 88000.00,
        "account_status": "Active",
        "account_type": "Investment",
    },
    "100005": {
        "name": "Eva Chen",
        "balance": 0.00,
        "account_status": "Closed",
        "account_type": "Checking",
    },
    "999999": {
        "name": "Test User",
        "balance": 1234.56,
        "account_status": "Active",
        "account_type": "Savings",
    },
}

# Live mutable copy — mutations here persist for the process lifetime
MEMBERS: dict[str, dict] = copy.deepcopy(_SEED)

# Audit log — every update is appended here
AUDIT_LOG: list[dict] = []


# ── Public helpers ─────────────────────────────────────────────────────────

def get_member(member_id: str) -> Optional[dict]:
    """Return a shallow copy of a member record, or None."""
    record = MEMBERS.get(member_id)
    return dict(record) if record else None


def update_member_status(
    member_id: str,
    new_status: str,
    reason_code: str = "",
    operator: str = "automation",
) -> bool:
    """
    Mutate account_status in place.
    Returns True on success, False if member not found.
    """
    record = MEMBERS.get(member_id)
    if record is None:
        return False

    old_status = record["account_status"]
    record["account_status"] = new_status

    AUDIT_LOG.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "member_id": member_id,
        "member_name": record["name"],
        "field": "account_status",
        "old_value": old_status,
        "new_value": new_status,
        "reason_code": reason_code,
        "operator": operator,
    })
    return True


def reset_to_seed() -> None:
    """Restore seed data (useful for testing)."""
    global MEMBERS
    MEMBERS.clear()
    MEMBERS.update(copy.deepcopy(_SEED))
    AUDIT_LOG.clear()


def get_audit_log() -> list[dict]:
    return list(AUDIT_LOG)


def list_members() -> list[dict]:
    return [{"id": mid, **data} for mid, data in MEMBERS.items()]
