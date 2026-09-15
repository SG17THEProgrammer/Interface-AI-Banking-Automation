"""
hitl/controller.py
------------------
Manages the pause → human-acts → resume lifecycle.

The SAME Playwright Page object stays alive during escalation.
The operator works in the exact browser instance, preserving session state.
"""

from __future__ import annotations
import json
import logging
import os
import textwrap
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

from hitl.annotator import annotate_screenshot

logger = logging.getLogger(__name__)


@dataclass
class InterventionRequest:
    request_id: str
    capability_name: str
    goal: str
    current_step_id: str
    current_step_description: str
    reason: str
    current_url: str
    screenshot_path: Optional[str]
    context: dict
    severity: str = "medium"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "pending"
    human_notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, directory: str) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"intervention_{self.request_id}.json")
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        return path


class HITLController:
    def __init__(self, evidence_dir: str = "evidence"):
        self.evidence_dir = evidence_dir
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"HITL_{ts}_{self._counter:03d}"

    def _take_screenshot(self, page: "Page", request_id: str) -> Optional[str]:
        try:
            ss_dir = os.path.join(self.evidence_dir, "screenshots")
            os.makedirs(ss_dir, exist_ok=True)
            path = os.path.join(ss_dir, f"{request_id}_raw.png")
            page.screenshot(path=path, full_page=False)
            return path
        except Exception as e:
            logger.warning(f"[hitl] Screenshot failed: {e}")
            return None

    def escalate(
        self,
        page: "Page",
        capability_name: str,
        goal: str,
        current_step_id: str,
        current_step_description: str,
        reason: str,
        context: dict = None,
        severity: str = "medium",
        non_interactive: bool = False,
    ) -> dict:
        request_id  = self._next_id()
        current_url = page.url

        # Screenshot + annotation
        raw_path = self._take_screenshot(page, request_id)
        screenshot_path = raw_path
        if raw_path:
            ss_dir = os.path.join(self.evidence_dir, "screenshots")
            annotated = os.path.join(ss_dir, f"{request_id}_annotated.png")
            screenshot_path = annotate_screenshot(
                raw_path, current_step_id, reason, current_url, annotated
            )

        # Persist intervention record
        req = InterventionRequest(
            request_id=request_id,
            capability_name=capability_name,
            goal=goal,
            current_step_id=current_step_id,
            current_step_description=current_step_description,
            reason=reason,
            current_url=current_url,
            screenshot_path=screenshot_path,
            context=context or {},
            severity=severity,
        )
        interventions_dir = os.path.join(self.evidence_dir, "interventions")
        req.save(interventions_dir)
        logger.warning(f"[hitl] Intervention {request_id} saved")

        # Non-interactive mode — auto-resolve immediately (CI / tests)
        if non_interactive:
            req.status = "resolved"
            req.human_notes = "Auto-resolved in non-interactive mode"
            req.save(interventions_dir)
            return {"resolved": True, "url_after": current_url,
                    "human_notes": "auto-resolved", "duration_seconds": 0}

        # Interactive mode — print notice, block on input()
        self._print_intervention_notice(req, screenshot_path)
        req.status = "in_progress"
        req.save(interventions_dir)

        start = time.time()
        try:
            human_notes = input("  Your notes (optional), then press ENTER: ").strip()
        except (KeyboardInterrupt, EOFError):
            human_notes = "interrupted"

        duration   = time.time() - start
        url_after  = page.url
        req.status = "resolved"
        req.human_notes = human_notes
        req.save(interventions_dir)

        print(f"\n  [OK] Control returned to automation. Resuming from: {url_after}\n")
        logger.info(f"[hitl] {request_id} resolved in {duration:.1f}s")

        return {"resolved": True, "url_after": url_after,
                "human_notes": human_notes, "duration_seconds": duration}

    def detect_stuck(
        self,
        consecutive_failures: int,
        last_error: str,
        threshold: int = 3,
    ) -> bool:
        if consecutive_failures >= threshold:
            logger.warning(f"[hitl] Stuck after {consecutive_failures} failures: {last_error}")
            return True
        return False

    @staticmethod
    def _print_intervention_notice(req: InterventionRequest, screenshot_path: Optional[str]):
        print("\n")
        print("+" + "=" * 68 + "+")
        print("|  [!!]  HUMAN INTERVENTION REQUIRED" + " " * 33 + "|")
        print("+" + "=" * 68 + "+")
        print(f"|  Request ID  : {req.request_id:<52}|")
        print(f"|  Capability  : {req.capability_name:<52}|")
        print(f"|  Stuck step  : {req.current_step_id:<52}|")
        print("+" + "=" * 68 + "+")
        for line in textwrap.wrap(f"Reason: {req.reason}", width=66)[:4]:
            print(f"|  {line:<66}|")
        print("+" + "=" * 68 + "+")
        print(f"|  URL: {req.current_url[:61]:<61}|")
        if screenshot_path:
            short = os.path.basename(screenshot_path)
            print(f"|  Screenshot: {short:<55}|")
        print("+" + "=" * 68 + "+")
        print("|  The browser window is OPEN. Fix the issue, then press ENTER. |")
        print("+" + "=" * 68 + "+\n")
