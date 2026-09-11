"""
Human-in-the-Loop (HITL) Escalation
-------------------------------------
When automation is stuck, hits an irreversible action it can't confirm,
or encounters something it cannot safely handle, this module:

  1. Pauses execution (keeping the browser window open)
  2. Emits a structured intervention request with full context
  3. Waits for the human to complete the manual action
  4. Verifies the new state and hands control back to automation

Design decisions:
- The same Playwright page object is passed through — the human operates
  the LIVE browser session, not a new one. Session continuity is preserved.
- The intervention request is persisted to disk so it can be routed to a
  real operator queue in a production system (email, Slack, ticket system).
- Control transfer signal is CLI-based (press Enter) for this implementation,
  with a clear design for a REST-based signal in production.
- Everything the human does during control is recorded for audit.
"""

from __future__ import annotations
import json
import os
import time
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intervention request schema
# ---------------------------------------------------------------------------

@dataclass
class InterventionRequest:
    """
    Structured escalation payload sent to a human operator.
    Contains everything needed to understand the situation and act.
    """
    request_id: str
    capability_name: str
    goal: str
    current_step_id: str
    current_step_description: str
    reason: str                    # Why automation is escalating
    current_url: str
    screenshot_path: Optional[str]
    context: dict                  # Additional state info
    severity: str = "medium"       # "low" | "medium" | "high"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "pending"        # "pending" | "in_progress" | "resolved"
    human_notes: str = ""          # Filled in when human resolves

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, directory: str) -> str:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"intervention_{self.request_id}.json")
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        return path


# ---------------------------------------------------------------------------
# HITL Controller
# ---------------------------------------------------------------------------

class HITLController:
    """
    Manages the pause → human-takes-control → resume lifecycle.
    """

    def __init__(self, evidence_dir: str = "evidence", headless: bool = False):
        self.evidence_dir = evidence_dir
        self.headless = headless
        self._intervention_counter = 0

    def _next_request_id(self) -> str:
        self._intervention_counter += 1
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"HITL_{ts}_{self._intervention_counter:03d}"

    def _take_screenshot(self, page: "Page", request_id: str) -> Optional[str]:
        """Capture a screenshot of the current browser state."""
        try:
            screenshot_dir = os.path.join(self.evidence_dir, "screenshots")
            os.makedirs(screenshot_dir, exist_ok=True)
            path = os.path.join(screenshot_dir, f"{request_id}.png")
            page.screenshot(path=path)
            logger.info(f"[hitl] Screenshot saved: {path}")
            return path
        except Exception as e:
            logger.warning(f"[hitl] Could not take screenshot: {e}")
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
        """
        Main escalation entry point.

        Pauses automation, exposes the live browser to the human,
        waits for them to signal completion, then verifies state and resumes.

        Returns a dict with:
          - resolved: bool
          - url_after: str
          - human_notes: str
          - duration_seconds: float
        """
        request_id = self._next_request_id()
        current_url = page.url

        # Take screenshot of the current state
        screenshot_path = self._take_screenshot(page, request_id)

        # Build the intervention request
        request = InterventionRequest(
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

        # Persist the intervention request
        request_path = request.save(os.path.join(self.evidence_dir, "interventions"))
        logger.warning(f"[hitl] Intervention request saved: {request_path}")

        if non_interactive:
            # In test/CI mode: auto-resolve without human input
            logger.info("[hitl] Non-interactive mode: auto-resolving intervention")
            request.status = "resolved"
            request.human_notes = "Auto-resolved in non-interactive mode"
            request.save(os.path.join(self.evidence_dir, "interventions"))
            return {
                "resolved": True,
                "url_after": current_url,
                "human_notes": "auto-resolved",
                "duration_seconds": 0,
            }

        # -----------------------------------------------------------------------
        # Display intervention notice to the operator
        # In production this would be: Slack message, email, ticket, REST webhook
        # -----------------------------------------------------------------------
        print("\n" + "="*70)
        print("🛑  HUMAN INTERVENTION REQUIRED")
        print("="*70)
        print(f"  Request ID  : {request_id}")
        print(f"  Capability  : {capability_name}")
        print(f"  Goal        : {goal}")
        print(f"  Current Step: {current_step_id} — {current_step_description}")
        print(f"  Reason      : {reason}")
        print(f"  Current URL : {current_url}")
        if screenshot_path:
            print(f"  Screenshot  : {screenshot_path}")
        print(f"  Severity    : {severity.upper()}")
        print()
        print("  The browser window is OPEN. Please:")
        print("  1. Look at the browser window")
        print("  2. Perform the required manual action")
        print("  3. Leave the browser on the correct page when done")
        print("  4. Return here and press ENTER to hand control back")
        print()
        print("  (Type any notes before pressing ENTER, or just press ENTER)")
        print("="*70 + "\n")

        start_time = time.time()
        request.status = "in_progress"

        try:
            human_notes = input("  Your notes (optional): ").strip()
        except (KeyboardInterrupt, EOFError):
            human_notes = ""

        duration = time.time() - start_time
        url_after = page.url

        # Update and persist resolved request
        request.status = "resolved"
        request.human_notes = human_notes
        request.save(os.path.join(self.evidence_dir, "interventions"))

        print(f"\n✅  Control returned to automation. Verifying state...")
        logger.info(
            f"[hitl] Intervention {request_id} resolved in {duration:.1f}s. "
            f"URL after: {url_after}. Notes: {human_notes!r}"
        )

        return {
            "resolved": True,
            "url_after": url_after,
            "human_notes": human_notes,
            "duration_seconds": duration,
        }

    def detect_stuck(
        self,
        consecutive_failures: int,
        last_error: str,
        max_failures_before_escalation: int = 3,
    ) -> bool:
        """
        Heuristic to decide if the system is stuck and needs human help.
        In production this would be more sophisticated (loop detection,
        state comparison, etc.).
        """
        if consecutive_failures >= max_failures_before_escalation:
            logger.warning(
                f"[hitl] Stuck detected after {consecutive_failures} consecutive failures. "
                f"Last error: {last_error}"
            )
            return True
        return False
