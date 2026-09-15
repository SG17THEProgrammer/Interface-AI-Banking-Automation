"""
engine/extractor.py
--------------------
Extracts declared output fields from a page and coerces them to typed values.
Also handles business-outcome detection and recoverable-condition dismissal.
"""

from __future__ import annotations
import re
import logging
from typing import Any, Optional

from playwright.sync_api import Page

from engine.locator import resolve_element

logger = logging.getLogger(__name__)

# ── Business outcome patterns ──────────────────────────────────────────────

BUSINESS_OUTCOME_PATTERNS = [
    re.compile(r"no record found",  re.IGNORECASE),
    re.compile(r"member not found", re.IGNORECASE),
    re.compile(r"not found",        re.IGNORECASE),
    re.compile(r"no results",       re.IGNORECASE),
    re.compile(r"account not found",re.IGNORECASE),
    re.compile(r"invalid member",   re.IGNORECASE),
    re.compile(r"permission denied",re.IGNORECASE),
    re.compile(r"access denied",    re.IGNORECASE),
]

# ── Recoverable conditions ────────────────────────────────────────────────

RECOVERABLE_SELECTORS = [
    "[id*='cookie'] button, [class*='cookie'] button, button[aria-label*='Accept']",
    "button[aria-label='Close'], button[aria-label='Dismiss'], .modal-close",
]


def check_for_business_outcome(page: Page) -> Optional[str]:
    """
    Scan the page text for known non-error outcomes (e.g. member not found).
    Returns a human-readable description if found, else None.
    """
    try:
        body_text = page.locator("body").inner_text(timeout=2000)
        for pattern in BUSINESS_OUTCOME_PATTERNS:
            if pattern.search(body_text):
                # Try to grab the specific element text for a cleaner message
                try:
                    el = page.locator("#member-not-found")
                    el.wait_for(state="visible", timeout=1000)
                    return el.inner_text().strip()
                except Exception:
                    return f"Business outcome: {pattern.pattern}"
    except Exception:
        pass
    return None


def try_recover_page(page: Page) -> bool:
    """
    Dismiss common blocking elements (cookie banners, stray modals).
    Returns True if something was dismissed.
    """
    for selector in RECOVERABLE_SELECTORS:
        try:
            el = page.locator(selector)
            if el.count() > 0:
                el.first.click()
                page.wait_for_timeout(300)
                logger.info(f"[extractor] Dismissed blocking element: {selector}")
                return True
        except Exception:
            pass
    return False


def extract_output(page: Page, output_field, guardrails) -> Any:
    """
    Extract one declared output field.
    Applies type coercion ($4,521.00 → 4521.0) and PII redaction.
    """
    el, strategy, value = resolve_element(page, output_field.locators)
    raw_text = el.inner_text().strip()

    # PII redaction before logging (value is still returned unredacted to caller)
    pii_fields = getattr(guardrails.policy, "pii_fields", [])
    if output_field.name in pii_fields:
        logger.info(f"[extractor] field={output_field.name} value=[REDACTED]")
    else:
        logger.info(f"[extractor] field={output_field.name} raw={raw_text!r}")

    return _coerce(raw_text, output_field.type)


def _coerce(raw: str, target_type: str) -> Any:
    try:
        if target_type == "float":
            cleaned = re.sub(r"[^0-9.\-]", "", raw)
            return float(cleaned) if cleaned else None
        elif target_type == "int":
            cleaned = re.sub(r"[^0-9\-]", "", raw)
            return int(cleaned) if cleaned else None
        elif target_type == "boolean":
            return raw.lower() in ("true", "yes", "1", "on", "active")
        else:
            return raw
    except (ValueError, TypeError):
        return raw
