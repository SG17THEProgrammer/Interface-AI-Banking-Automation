"""
Engine wrapper — per-job evidence isolation
--------------------------------------------
Wraps ReplayEngine so that every chat message gets its own evidence
directory: evidence/jobs/{job_id}/

Changes vs the original replay.py:
  - ReplayEngine.run() gains an optional `job_dir` kwarg.
  - When `job_dir` is supplied all output goes there:
      {job_dir}/replay.log
      {job_dir}/screenshots/
      {job_dir}/interventions/
  - When `job_dir` is None behaviour is identical to the original
    (writes to the global evidence/ directory).
  - The `job_id` string is also accepted as a convenience alias;
    the caller can pass either.

This module re-exports the full public surface so existing code that
does `from replay import ReplayEngine` keeps working unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import logging
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

# Pull in everything from the existing replay module so callers can keep
# importing from here if they like.
import sys
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from artifact import CapabilityArtifact, Step, OutputField
from guardrails import Guardrails, GuardrailViolation, DEFAULT_POLICY
from hitl import HITLController

# Re-import the result type and helpers from replay so nothing breaks.
from replay import (
    ReplayResult,
    resolve_element,
    check_for_business_outcome,
    try_recover_page,
    extract_output,
    execute_step,
    verify_checkpoint,
    RECOVERABLE_PATTERNS,
    BUSINESS_OUTCOME_PATTERNS,
)

logger = logging.getLogger(__name__)


class JobReplayEngine:
    """
    Drop-in replacement for ReplayEngine that writes all evidence
    (logs, screenshots, interventions) into a per-job directory.

    Usage
    -----
    engine = JobReplayEngine(base_evidence_dir="evidence", headless=True)
    result = engine.run(artifact, parameters={"member_id": "100001"},
                        job_id="job_abc123")

    All evidence lands in evidence/jobs/job_abc123/.
    """

    def __init__(
        self,
        base_evidence_dir: str = "evidence",
        headless: bool = True,
        max_retries_per_step: int = 2,
        hitl_enabled: bool = True,
    ):
        self.base_evidence_dir = base_evidence_dir
        self.headless = headless
        self.max_retries = max_retries_per_step
        self.hitl_enabled = hitl_enabled
        self.guardrails = Guardrails(DEFAULT_POLICY)

    def _job_dir(self, job_id: str) -> str:
        d = os.path.join(self.base_evidence_dir, "jobs", job_id)
        os.makedirs(os.path.join(d, "screenshots"), exist_ok=True)
        os.makedirs(os.path.join(d, "interventions"), exist_ok=True)
        return d

    def run(
        self,
        artifact: CapabilityArtifact,
        parameters: dict,
        job_id: str = "default",
        log_suffix: str = "replay",        # kept for compatibility
        non_interactive: bool = True,
    ) -> ReplayResult:
        """Execute artifact and write all evidence into evidence/jobs/{job_id}/."""

        job_dir = self._job_dir(job_id)

        # HITL controller writes interventions and screenshots into job_dir
        hitl = HITLController(evidence_dir=job_dir)

        start_time = time.time()
        run_log: list[dict] = []
        steps_completed = 0
        retries_used = 0

        def log(entry: dict):
            entry["ts"] = datetime.now(timezone.utc).isoformat()
            run_log.append(entry)
            logger.info(f"[engine] {json.dumps(entry)}")

        log({
            "event": "replay_start",
            "capability": artifact.name,
            "job_id": job_id,
            "params": self.guardrails.redact_dict(parameters),
        })

        # Validate parameters
        try:
            self.guardrails.validate_parameters(parameters, artifact.parameters)
        except ValueError as e:
            self._save_log(run_log, job_dir, "hard_failure")
            return ReplayResult(
                outcome_type="hard_failure",
                success=False,
                outputs={},
                error=str(e),
                duration_seconds=time.time() - start_time,
                log_entries=run_log,
            )

        extracted = {}
        screenshot_path = None

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--start-maximized"],
                slow_mo=300 if not self.headless else 0,
            )
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()

            try:
                for step in artifact.steps:
                    log({"event": "step_start", "step_id": step.step_id,
                         "action": step.action, "url": page.url})

                    # Business-outcome check before extract/assert steps
                    if step.action in ("assert", "extract") or steps_completed > 2:
                        outcome = check_for_business_outcome(page)
                        if outcome:
                            log({"event": "business_outcome_detected", "outcome": outcome})
                            self._save_log(run_log, job_dir, "business_outcome")
                            browser.close()
                            return ReplayResult(
                                outcome_type="business_outcome",
                                success=False,
                                outputs={},
                                business_outcome=outcome,
                                steps_completed=steps_completed,
                                duration_seconds=time.time() - start_time,
                                log_entries=run_log,
                            )

                    if try_recover_page(page):
                        log({"event": "recovered_blocking_condition", "step": step.step_id})
                        retries_used += 1

                    # Execute with retry
                    step_result = None
                    for attempt in range(self.max_retries + 1):
                        step_result = execute_step(page, step, parameters, self.guardrails)
                        if step_result.get("success"):
                            break
                        if attempt < self.max_retries:
                            log({"event": "step_retry", "step_id": step.step_id,
                                 "attempt": attempt + 1, "error": step_result.get("error")})
                            page.wait_for_timeout(1000)
                            retries_used += 1
                            try_recover_page(page)

                    log({"event": "step_done", "step_id": step.step_id, "result": step_result})

                    if not step_result.get("success"):
                        screenshot_path = self._screenshot(
                            page, job_dir, f"failure_{step.step_id}",
                            step_id=step.step_id,
                            reason=step_result.get("error", "Step failed"),
                        )
                        log({"event": "step_failed", "step_id": step.step_id,
                             "error": step_result.get("error"),
                             "screenshot": screenshot_path})

                        if step_result.get("blocked"):
                            self._save_log(run_log, job_dir, log_suffix)
                            browser.close()
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

                        # HITL escalation
                        if self.hitl_enabled:
                            hitl_result = hitl.escalate(
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

                        self._save_log(run_log, job_dir, "failure")
                        browser.close()
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

                # All steps done — extract outputs
                log({"event": "extracting_outputs"})
                for output_field in artifact.outputs:
                    try:
                        value = extract_output(page, output_field, self.guardrails)
                        extracted[output_field.name] = value
                        log({"event": "output_extracted",
                             "field": output_field.name, "value": str(value)[:100]})
                    except Exception as e:
                        log({"event": "output_extraction_failed",
                             "field": output_field.name, "error": str(e)})
                        extracted[output_field.name] = None

                screenshot_path = self._screenshot(page, job_dir, "replay_final")
                log({"event": "replay_complete", "outputs": extracted, "steps": steps_completed})

            except Exception as e:
                screenshot_path = self._screenshot(page, job_dir, "replay_error")
                log({"event": "replay_error", "error": str(e),
                     "trace": traceback.format_exc()})
                self._save_log(run_log, job_dir, "failure")
                browser.close()
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
                try:
                    browser.close()
                except Exception:
                    pass

        self._save_log(run_log, job_dir, log_suffix)
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _screenshot(
        self, page: Page, job_dir: str, name: str,
        step_id: str = "", reason: str = "",
    ) -> str:
        try:
            ss_dir = os.path.join(job_dir, "screenshots")
            os.makedirs(ss_dir, exist_ok=True)
            path = os.path.join(ss_dir, f"{name}.png")
            page.screenshot(path=path)
            if reason and ("failure" in name or "hitl" in name.lower()):
                from hitl import annotate_screenshot
                annotate_screenshot(path, step_id or name, reason, page.url, path)
            return path
        except Exception:
            return ""

    def _save_log(self, run_log: list, job_dir: str, suffix: str) -> str:
        path = os.path.join(job_dir, f"replay_{suffix}.log")
        with open(path, "w", encoding="utf-8") as f:
            for entry in run_log:
                f.write(json.dumps(entry) + "\n")
        return path