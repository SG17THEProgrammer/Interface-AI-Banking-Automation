"""
engine/locator.py
-----------------
Element resolution with ordered fallback chains.
Each step carries multiple locator strategies; this module tries them
in order and returns the first one that resolves to a visible element.
"""

from __future__ import annotations
import logging
from typing import Optional, Tuple

from playwright.sync_api import Page

logger = logging.getLogger(__name__)


def resolve_element(page: Page, locators: list, timeout: int = 5000):
    """
    Try each locator in order. Returns (element, strategy, value).
    Raises RuntimeError if all strategies are exhausted.
    """
    last_error = None
    for loc in locators:
        strategy = loc.strategy if hasattr(loc, "strategy") else loc.get("strategy", "css")
        value    = loc.value    if hasattr(loc, "value")    else loc.get("value", "")
        if not value:
            continue
        try:
            el = _build_locator(page, strategy, value)
            el.wait_for(state="visible", timeout=timeout)
            logger.debug(f"[locator] Resolved via {strategy}={value!r}")
            return el, strategy, value
        except Exception as e:
            last_error = e
            logger.debug(f"[locator] Failed {strategy}={value!r}: {e}")

    raise RuntimeError(
        f"All {len(locators)} locator(s) exhausted. Last error: {last_error}"
    )


def _build_locator(page: Page, strategy: str, value: str):
    dispatch = {
        "css":         lambda v: page.locator(v).first,
        "text":        lambda v: page.get_by_text(v, exact=False).first,
        "aria-label":  lambda v: page.get_by_label(v).first,
        "placeholder": lambda v: page.get_by_placeholder(v).first,
        "xpath":       lambda v: page.locator(f"xpath={v}").first,
    }
    builder = dispatch.get(strategy, lambda v: page.locator(v).first)
    return builder(value)
