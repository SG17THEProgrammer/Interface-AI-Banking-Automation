"""
engine/executor.py
------------------
Executes a single Step from a CapabilityArtifact.
Returns a result dict; never raises — errors are captured in result["error"].
"""

from __future__ import annotations
import logging
from typing import Optional

from playwright.sync_api import Page

from engine.locator import resolve_element
from guardrails import Guardrails, GuardrailViolation

logger = logging.getLogger(__name__)


def execute_step(
    page: Page,
    step,
    params: dict,
    guardrails: Guardrails,
) -> dict:
    """
    Execute one step. Returns dict with at minimum:
      { step_id, action, success: bool }
    Plus action-specific keys (url, strategy_used, typed, etc.)
    """
    result = {"step_id": step.step_id, "action": step.action, "success": False}

    # Resolve runtime input value
    value = step.input_value
    if step.input_var and step.input_var in params:
        value = str(params[step.input_var])

    try:
        guardrails.check_action(step.action, step.is_reversible, step.step_id)

        if step.action == "navigate":
            _do_navigate(page, value, guardrails, result)

        elif step.action == "type":
            _do_type(page, step, value, result)

        elif step.action == "click":
            _do_click(page, step, result)

        elif step.action == "select":
            _do_select(page, step, value, result)

        elif step.action == "wait":
            _do_wait(page, step, result)

        elif step.action == "assert":
            if step.checkpoint:
                verify_checkpoint(page, step.checkpoint)
            result["success"] = True

        elif step.action == "extract":
            # Extraction is handled in bulk after all steps complete
            result["success"] = True

        else:
            result["error"] = f"Unknown action: {step.action}"
            return result

        # Checkpoint after non-assert actions
        if step.checkpoint and step.action != "assert":
            verify_checkpoint(page, step.checkpoint)
            result["checkpoint_passed"] = True

        page.wait_for_timeout(step.wait_after_ms)

    except GuardrailViolation as e:
        result["error"] = f"GUARDRAIL: {e}"
        result["blocked"] = True
    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"[executor] Step {step.step_id} failed: {e}")

    return result


# ── Action implementations ─────────────────────────────────────────────────

def _do_navigate(page: Page, url: str, guardrails: Guardrails, result: dict):
    guardrails.check_url(url)
    page.goto(url, wait_until="domcontentloaded", timeout=15000)
    page.wait_for_timeout(500)
    result.update({"success": True, "url": page.url})


def _do_type(page: Page, step, value: str, result: dict):
    if not step.locators:
        raise RuntimeError(f"Step {step.step_id}: no locators for type action")
    el, strat, _ = resolve_element(page, step.locators)
    el.fill("")
    el.type(str(value) if value else "")
    result.update({"success": True, "strategy_used": strat, "typed": value})


def _do_click(page: Page, step, result: dict):
    if not step.locators:
        raise RuntimeError(f"Step {step.step_id}: no locators for click action")
    el, strat, _ = resolve_element(page, step.locators)
    el.click()
    result.update({"success": True, "strategy_used": strat})


def _do_select(page: Page, step, value: str, result: dict):
    """Select an option from a <select> element."""
    if not step.locators:
        raise RuntimeError(f"Step {step.step_id}: no locators for select action")
    el, strat, _ = resolve_element(page, step.locators)
    el.select_option(value=value)
    result.update({"success": True, "strategy_used": strat, "selected": value})


def _do_wait(page: Page, step, result: dict):
    if step.locators:
        selector = step.locators[0].value
        page.wait_for_selector(selector, timeout=10000)
    else:
        page.wait_for_timeout(step.wait_after_ms)
    result["success"] = True


# ── Checkpoint verification ────────────────────────────────────────────────

def verify_checkpoint(page: Page, checkpoint) -> None:
    """Assert a post-step condition. Raises AssertionError on failure."""
    ctype  = checkpoint.type
    cvalue = checkpoint.value

    if ctype == "url_contains":
        current = page.url
        if cvalue not in current:
            raise AssertionError(
                f"URL should contain '{cvalue}' but got '{current}'"
            )

    elif ctype == "element_visible":
        selectors = [s.strip() for s in cvalue.split(",")]
        found = False
        for sel in selectors:
            try:
                el = page.locator(sel)
                if el.count() > 0:
                    el.first.wait_for(state="visible", timeout=3000)
                    found = True
                    break
            except Exception:
                continue
        if not found:
            raise AssertionError(
                f"None of {selectors} visible on {page.url}"
            )

    elif ctype == "element_text_contains":
        sel, expected = cvalue.split("::", 1) if "::" in cvalue else (cvalue, "")
        if expected:
            text = page.locator(sel).first.inner_text()
            if expected.lower() not in text.lower():
                raise AssertionError(
                    f"'{sel}' text should contain '{expected}' but got '{text}'"
                )

    elif ctype == "page_title_contains":
        title = page.title()
        if cvalue.lower() not in title.lower():
            raise AssertionError(
                f"Title should contain '{cvalue}' but got '{title}'"
            )
