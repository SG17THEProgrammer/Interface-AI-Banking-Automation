"""
Deterministic Replay Engine — Production Execution Path
---------------------------------------------------------
Takes a saved CapabilityArtifact + runtime parameters and executes the
recorded flow WITHOUT any LLM involvement.

This is what an AI agent calls in production. It must be:
- Deterministic: same inputs → same outputs every time
- Robust: handles runtime errors, not just the happy path
- Typed: returns a structured result the caller can parse

Error taxonomy (the most important design decision here):
  1. BUSINESS OUTCOME — a legitimate result the caller needs to know about.
     Example: "Member not found". This is NOT a crash. The caller receives
     a structured response with outcome_type="business_outcome".

  2. RECOVERABLE CONDITION — a transient state the system can handle itself.
     Example: spinner still showing, cookie banner blocking, brief 503.
     The engine dismisses/retries and continues.

  3. HARD FAILURE — something genuinely broken that stops execution.
     Example: the Search button literally doesn't exist, a network error.
     Returns a structured error with step, expected, observed, screenshot.

Design decisions:
- Multi-strategy locator chain: tries each locator in order, keeping the
  one that works. Means replay survives minor markup changes.
- Checkpoint assertions after every significant step: we verify we actually
  reached the expected state, not just that Playwright didn't throw.
- HITL escalation is available from replay too: if the engine hits a hard
  failure it can't recover from, it can hand off to a human.
"""

from __future__ import annotations
import json
import os
import re
import time
import logging
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PWTimeout

from artifact import CapabilityArtifact, Step, OutputField
from guardrails import Guardrails, GuardrailViolation, DEFAULT_POLICY
from hitl import HITLController

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ReplayResult:
    """
    The structured result returned to the caller after a replay run.
    The caller should check outcome_type first, then read outputs or error.
    """
    outcome_type: str          # "success" | "business_outcome" | "recoverable_retried" | "hard_failure" | "hitl_escalated"
    success: bool
    # Typed data extracted during replay (only on success)
    outputs: dict
    # Human-readable outcome if not success
    business_outcome: Optional[str] = None
    error: Optional[str] = None              # Error message on hard failure
    failed_step_id: Optional[str] = None
    expected_state: Optional[str] = None
    observed_state: Optional[str] = None
    screenshot_path: Optional[str] = None
    steps_completed: int = 0
    duration_seconds: float = 0.0
    log_entries: list = None
    retries_used: int = 0

    def __post_init__(self):
        if self.log_entries is None:
            self.log_entries = []

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Known recoverable conditions
# ---------------------------------------------------------------------------

RECOVERABLE_PATTERNS = [
    # Cookie/GDPR banners
    {"selector": "[id*='cookie'] button, [class*='cookie'] button, button[aria-label*='Accept']",
     "action": "click", "description": "Dismiss cookie banner"},
    # Generic dismiss buttons
    {"selector": "button[aria-label='Close'], button[aria-label='Dismiss'], .modal-close",
     "action": "click", "description": "Dismiss modal"},
]

BUSINESS_OUTCOME_PATTERNS = [
    re.compile(r"no record found", re.IGNORECASE),
    re.compile(r"member not found", re.IGNORECASE),
    re.compile(r"not found", re.IGNORECASE),
    re.compile(r"no results", re.IGNORECASE),
    re.compile(r"account not found", re.IGNORECASE),
    re.compile(r"invalid member", re.IGNORECASE),
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"access denied", re.IGNORECASE),
    re.compile(r"account frozen", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Element resolver with fallback chain
# ---------------------------------------------------------------------------

def resolve_element(page: Page, locators: list, timeout: int = 5000):
    """
    Try each locator in order. Returns (element, strategy, value).
    Raises RuntimeError if all fail.
    """
    last_error = None
    for loc in locators:
        strategy = loc.strategy
        value = loc.value
        try:
            if strategy == "css":
                el = page.locator(value).first
            elif strategy == "text":
                el = page.get_by_text(value, exact=False).first
            elif strategy == "aria-label":
                el = page.get_by_label(value).first
            elif strategy == "placeholder":
                el = page.get_by_placeholder(value).first
            elif strategy == "xpath":
                el = page.locator(f"xpath={value}").first
            else:
                el = page.locator(value).first

            el.wait_for(state="visible", timeout=timeout)
            return el, strategy, value
        except Exception as e:
            last_error = e
            logger.debug(f"Locator failed ({strategy}={value!r}): {e}")

    raise RuntimeError(
        f"All {len(locators)} locator(s) exhausted. Last error: {last_error}"
    )


def check_for_business_outcome(page: Page) -> Optional[str]:
    """
    Scan the page for known business outcome messages.
    Returns a description string if found, None otherwise.
    """
    try:
        body_text = page.locator("body").inner_text(timeout=2000)
        for pattern in BUSINESS_OUTCOME_PATTERNS:
            if pattern.search(body_text):
                # Also check for the specific not-found element
                try:
                    el = page.locator("#member-not-found")
                    el.wait_for(state="visible", timeout=1000)
                    text = el.inner_text()
                    return text.strip()
                except Exception:
                    return f"Business outcome detected: {pattern.pattern}"
    except Exception:
        pass
    return None


def try_recover_page(page: Page) -> bool:
    """
    Attempt to recover from common transient blocking conditions.
    Returns True if something was dismissed/recovered.
    """
    for pattern in RECOVERABLE_PATTERNS:
        try:
            el = page.locator(pattern["selector"])
            if el.count() > 0:
                el.first.click()
                page.wait_for_timeout(300)
                logger.info(f"[replay] Recovered: {pattern['description']}")
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Output extractor
# ---------------------------------------------------------------------------

def extract_output(page: Page, output_field: OutputField, guardrails: Guardrails) -> Any:
    """
    Extract a single output value from the page.
    Applies type coercion and PII redaction.
    """
    el, strategy, value = resolve_element(page, output_field.locators)
    raw_text = el.inner_text().strip()

    # Redact if this is a PII field
    if output_field.name in (guardrails.policy.pii_fields if hasattr(guardrails.policy, 'pii_fields') else []):
        raw_text = guardrails.redact(raw_text)

    # Type coercion
    try:
        if output_field.type == "float":
            # Remove currency symbols and commas: "$4,521.00" → 4521.00
            cleaned = re.sub(r"[^0-9.\-]", "", raw_text)
            return float(cleaned) if cleaned else None
        elif output_field.type == "int":
            cleaned = re.sub(r"[^0-9\-]", "", raw_text)
            return int(cleaned) if cleaned else None
        elif output_field.type == "boolean":
            return raw_text.lower() in ("true", "yes", "1", "on", "active")
        else:
            return raw_text
    except (ValueError, TypeError):
        return raw_text  # Return raw if coercion fails


# ---------------------------------------------------------------------------
# Step executor
# ---------------------------------------------------------------------------

def execute_step(
    page: Page,
    step: Step,
    params: dict,
    guardrails: Guardrails,
) -> dict:
    """
    Execute a single step from the artifact.
    Returns a result dict with success, details, and any error info.
    """
    result = {"step_id": step.step_id, "action": step.action, "success": False}

    # Resolve the input value
    value = step.input_value
    if step.input_var and step.input_var in params:
        value = str(params[step.input_var])

    try:
        # Safety check before irreversible actions
        guardrails.check_action(step.action, step.is_reversible, step.step_id)

        if step.action == "navigate":
            url = value or step.input_value
            guardrails.check_url(url)
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(500)
            result.update({"success": True, "url": page.url})

        elif step.action == "type":
            if not step.locators:
                raise RuntimeError(
                    f"Step {step.step_id}: no locators defined for type action")
            el, strat, val = resolve_element(page, step.locators)
            el.fill("")
            el.type(str(value) if value else "")
            page.wait_for_timeout(step.wait_after_ms)
            result.update(
                {"success": True, "strategy_used": strat, "typed": value})

        elif step.action == "click":
            if not step.locators:
                raise RuntimeError(
                    f"Step {step.step_id}: no locators defined for click action")
            el, strat, val = resolve_element(page, step.locators)
            el.click()
            page.wait_for_timeout(step.wait_after_ms)
            result.update({"success": True, "strategy_used": strat})

        elif step.action == "wait":
            if step.locators:
                selector = step.locators[0].value
                page.wait_for_selector(selector, timeout=10000)
            else:
                page.wait_for_timeout(step.wait_after_ms)
            result["success"] = True

        elif step.action == "assert":
            # Check checkpoint condition
            if step.checkpoint:
                verify_checkpoint(page, step.checkpoint)
            result["success"] = True

        elif step.action == "extract":
            # Extract is handled separately in the output phase
            result["success"] = True

        else:
            result["error"] = f"Unknown action: {step.action}"
            return result

        # Verify checkpoint if defined
        if step.checkpoint and step.action != "assert":
            verify_checkpoint(page, step.checkpoint)
            result["checkpoint_passed"] = True

        page.wait_for_timeout(step.wait_after_ms)

    except GuardrailViolation as e:
        result["error"] = f"GUARDRAIL: {e}"
        result["blocked"] = True
    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"[replay] Step {step.step_id} failed: {e}")

    return result


def verify_checkpoint(page: Page, checkpoint) -> None:
    """
    Assert a checkpoint condition. Raises on failure.
    """
    ctype = checkpoint.type
    cvalue = checkpoint.value

    if ctype == "url_contains":
        current = page.url
        if cvalue not in current:
            raise AssertionError(
                f"Checkpoint failed: URL should contain '{cvalue}' but got '{current}'"
            )

    elif ctype == "element_visible":
        # Support comma-separated selectors (ANY must be visible)
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
                f"Checkpoint failed: none of {selectors} were visible on {page.url}"
            )

    elif ctype == "element_text_contains":
        # cvalue format: "selector::expected_text"
        parts = cvalue.split("::", 1)
        if len(parts) == 2:
            sel, expected = parts
            el = page.locator(sel).first
            text = el.inner_text()
            if expected.lower() not in text.lower():
                raise AssertionError(
                    f"Checkpoint failed: '{sel}' text should contain '{expected}' but got '{text}'"
                )

    elif ctype == "page_title_contains":
        title = page.title()
        if cvalue.lower() not in title.lower():
            raise AssertionError(
                f"Checkpoint failed: page title should contain '{cvalue}' but got '{title}'"
            )


# ---------------------------------------------------------------------------
# Main replay engine
# ---------------------------------------------------------------------------

class ReplayEngine:
    def __init__(
        self,
        evidence_dir: str = "evidence",
        headless: bool = True,
        max_retries_per_step: int = 2,
        hitl_enabled: bool = True,
    ):
        self.evidence_dir = evidence_dir
        self.headless = headless
        self.max_retries = max_retries_per_step
        self.hitl_enabled = hitl_enabled
        self.guardrails = Guardrails(DEFAULT_POLICY)
        self.hitl = HITLController(evidence_dir=evidence_dir)

    def run(
        self,
        artifact: CapabilityArtifact,
        parameters: dict,
        log_suffix: str = "success",
        non_interactive: bool = True,
    ) -> ReplayResult:
        """
        Execute a capability artifact deterministically.
        Returns a ReplayResult with typed outputs.
        """
        start_time = time.time()
        run_log = []
        steps_completed = 0
        retries_used = 0

        def log(entry: dict):
            entry["ts"] = datetime.now(timezone.utc).isoformat()
            run_log.append(entry)
            logger.info(f"[replay] {json.dumps(entry)}")

        log({"event": "replay_start", "capability": artifact.name,
            "params": self.guardrails.redact_dict(parameters)})

        # Validate parameters against schema
        try:
            self.guardrails.validate_parameters(
                parameters, artifact.parameters)
        except ValueError as e:
            self._save_log(run_log, "hard_failure")
            return ReplayResult(
                outcome_type="hard_failure",
                success=False,
                outputs={},
                error=str(e),
                duration_seconds=time.time() - start_time,
                log_entries=run_log,
            )

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox",
                      "--start-maximized"],
                slow_mo=500 if not self.headless else 0,   # 500ms so you can see each action
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 900})
            page = context.new_page()
            screenshot_path = None

            try:
                for step in artifact.steps:
                    log({"event": "step_start", "step_id": step.step_id,
                        "action": step.action, "url": page.url})

                    # Check for business outcomes before attempting the step
                    # (only after navigation, when a result page might be showing)
                    if step.action in ("assert", "extract") or steps_completed > 2:
                        outcome = check_for_business_outcome(page)
                        if outcome:
                            log({"event": "business_outcome_detected",
                                "outcome": outcome})
                            # Save log and return as business outcome
                            self._save_log(
                                run_log, log_suffix="business_outcome")
                            return ReplayResult(
                                outcome_type="business_outcome",
                                success=False,
                                outputs={},
                                business_outcome=outcome,
                                steps_completed=steps_completed,
                                duration_seconds=time.time() - start_time,
                                log_entries=run_log,
                            )

                    # Try to recover from any transient blocking conditions
                    if try_recover_page(page):
                        log({"event": "recovered_blocking_condition",
                            "step": step.step_id})
                        retries_used += 1

                    # Execute step with retry
                    step_result = None
                    for attempt in range(self.max_retries + 1):
                        step_result = execute_step(
                            page, step, parameters, self.guardrails)
                        if step_result.get("success"):
                            break
                        if attempt < self.max_retries:
                            log({"event": "step_retry", "step_id": step.step_id, "attempt": attempt + 1,
                                 "error": step_result.get("error")})
                            page.wait_for_timeout(1000)
                            retries_used += 1
                            try_recover_page(page)

                    log({"event": "step_done", "step_id": step.step_id,
                        "result": step_result})

                    if not step_result.get("success"):
                        # Take a failure screenshot
                        screenshot_path = self._screenshot(
                            page, f"failure_{step.step_id}",
                            step_id=step.step_id,
                            reason=step_result.get("error", "Step failed")
                        )
                        log({"event": "step_failed", "step_id": step.step_id,
                             "error": step_result.get("error"), "screenshot": screenshot_path})

                        # Check if blocked by guardrail
                        if step_result.get("blocked"):
                            self._save_log(run_log, log_suffix)
                            return ReplayResult(
                                outcome_type="hard_failure",
                                success=False,
                                outputs={},
                                error=step_result["error"],
                                failed_step_id=step.step_id,
                                expected_state=f"Step '{step.step_id}' to complete",
                                observed_state="Blocked by guardrail",
                                screenshot_path=screenshot_path,
                                steps_completed=steps_completed,
                                duration_seconds=time.time() - start_time,
                                log_entries=run_log,
                            )

                        # Try HITL escalation for hard failures
                        if self.hitl_enabled:
                            hitl_result = self.hitl.escalate(
                                page=page,
                                capability_name=artifact.name,
                                goal=artifact.description,
                                current_step_id=step.step_id,
                                current_step_description=step.description,
                                reason=step_result.get("error", "Step failed"),
                                context={"parameters": parameters,
                                         "steps_completed": steps_completed},
                                non_interactive=non_interactive,
                            )
                            if hitl_result["resolved"]:
                                log({"event": "hitl_resolved", "step_id": step.step_id,
                                     "url_after": hitl_result["url_after"]})
                                steps_completed += 1
                                continue

                        # Hard failure — no recovery
                        self._save_log(run_log, "failure")
                        return ReplayResult(
                            outcome_type="hard_failure",
                            success=False,
                            outputs={},
                            error=step_result.get("error"),
                            failed_step_id=step.step_id,
                            expected_state=step.description,
                            observed_state=f"Error: {step_result.get('error')}",
                            screenshot_path=screenshot_path,
                            steps_completed=steps_completed,
                            duration_seconds=time.time() - start_time,
                            log_entries=run_log,
                            retries_used=retries_used,
                        )

                    steps_completed += 1

                # ── All steps complete — extract outputs ──
                log({"event": "extracting_outputs"})
                extracted = {}
                for output_field in artifact.outputs:
                    try:
                        value = extract_output(
                            page, output_field, self.guardrails)
                        extracted[output_field.name] = value
                        log({"event": "output_extracted",
                            "field": output_field.name, "value": str(value)[:100]})
                    except Exception as e:
                        log({"event": "output_extraction_failed",
                            "field": output_field.name, "error": str(e)})
                        extracted[output_field.name] = None

                # Final screenshot
                screenshot_path = self._screenshot(page, "replay_final")
                log({"event": "replay_complete",
                    "outputs": extracted, "steps": steps_completed})

            except Exception as e:
                screenshot_path = self._screenshot(page, "replay_error")
                log({"event": "replay_error", "error": str(
                    e), "trace": traceback.format_exc()})
                self._save_log(run_log, "failure")
                return ReplayResult(
                    outcome_type="hard_failure",
                    success=False,
                    outputs={},
                    error=str(e),
                    screenshot_path=screenshot_path,
                    steps_completed=steps_completed,
                    duration_seconds=time.time() - start_time,
                    log_entries=run_log,
                )
            finally:
                browser.close()

        self._save_log(run_log, log_suffix)
        return ReplayResult(
            outcome_type="success",
            success=True,
            outputs=extracted,
            steps_completed=steps_completed,
            duration_seconds=time.time() - start_time,
            log_entries=run_log,
            screenshot_path=screenshot_path,
            retries_used=retries_used,
        )

    def _screenshot(self, page: Page, name: str,
                    step_id: str = "", reason: str = "") -> str:
        try:
            ss_dir = os.path.join(self.evidence_dir, "screenshots")
            os.makedirs(ss_dir, exist_ok=True)
            path = os.path.join(ss_dir, f"{name}.png")
            page.screenshot(path=path)
            if reason and ("failure" in name or "hitl" in name.lower()):
                from hitl import annotate_screenshot
                annotate_screenshot(path, step_id or name,
                                    reason, page.url, path)
            return path
        except Exception:
            return ""

    def _save_log(self, run_log: list, log_suffix: str) -> str:
        os.makedirs(self.evidence_dir, exist_ok=True)
        name = f"replay_{log_suffix}.log"
        path = os.path.join(self.evidence_dir, name)
        with open(path, "w") as f:
            for entry in run_log:
                f.write(json.dumps(entry) + "\n")
        return path
