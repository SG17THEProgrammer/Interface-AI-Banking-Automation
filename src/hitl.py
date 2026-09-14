"""
Human-in-the-Loop (HITL) Escalation
-------------------------------------
When automation is stuck, hits an irreversible action it can't confirm,
or encounters something it cannot safely handle, this module:

  1. Pauses execution -- keeps the browser window OPEN and VISIBLE
  2. Annotates a screenshot with exactly what went wrong (red box, error
     text, step ID, timestamp) so the human knows at a glance
  3. Prints a structured intervention notice to the terminal
  4. Blocks on input() -- the human fixes it in the live browser window
  5. Verifies the new page state and hands control back to automation

Design decisions:
- The SAME Playwright page object is passed through -- the human operates
  the LIVE browser session, not a new one. Session continuity is preserved.
- The browser is launched NON-HEADLESS when HITL is possible, so the
  window is actually visible for the human to interact with.
- Screenshots are annotated with PIL: red error banner, step ID, reason,
  timestamp -- raw screenshots are useless without context.
- The intervention request is persisted to disk (JSON) so it can be routed
  to Slack / ticketing / REST webhook in production.
- Control transfer signal is CLI-based (press Enter) for this implementation.
  In production, replace input() with a REST endpoint or WebSocket signal.
"""

from __future__ import annotations
import json
import os
import time
import textwrap
import logging
import sys as _sys
import os as _os
if _sys.platform == "win32":
    try:
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Screenshot annotation
# ---------------------------------------------------------------------------

def annotate_screenshot(
    raw_path: str,
    step_id: str,
    reason: str,
    current_url: str,
    out_path: str = None,
) -> str:
    """
    Annotate a screenshot with a red error banner showing what went wrong.
    Windows-first font loading (Consolas -> Arial -> Courier -> default).
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warning("[hitl] Pillow not installed - skipping annotation")
        return raw_path

    out_path = out_path or raw_path

    try:
        img = Image.open(raw_path).convert("RGBA")
        W, H = img.size

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        BANNER_H = 120
        BORDER   = 5

        # Dark semi-transparent banner across top
        draw.rectangle([(0, 0), (W, BANNER_H)], fill=(10, 5, 5, 220))

        # Red border around the whole image
        for t in range(BORDER):
            draw.rectangle([(t, t), (W-1-t, H-1-t)], outline=(210, 35, 35, 200))

        # Font loading - Windows paths first, then Linux, then default
        def load_font(size, bold=False):
            candidates = [
                "C:/Windows/Fonts/consolab.ttf" if bold else "C:/Windows/Fonts/consola.ttf",
                "C:/Windows/Fonts/arialbd.ttf"  if bold else "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/courbd.ttf"   if bold else "C:/Windows/Fonts/cour.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold
                    else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf" if bold
                    else "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
            ]
            for path in candidates:
                try:
                    return ImageFont.truetype(path, size)
                except Exception:
                    continue
            return ImageFont.load_default()

        font_title = load_font(15, bold=True)
        font_body  = load_font(12, bold=False)

        # Red alert bar at very top
        draw.rectangle([(0, 0), (W, 30)], fill=(185, 28, 28, 245))
        draw.text((12, 7), "[!!] AUTOMATION STUCK - HUMAN INTERVENTION REQUIRED",
                  font=font_title, fill=(255, 220, 220, 255))

        # Step + timestamp
        import datetime
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        draw.text((12, 38), f"Step: {step_id}    |    {ts}",
                  font=font_body, fill=(180, 180, 180, 220))

        # URL
        url_display = current_url[:115] + "..." if len(current_url) > 115 else current_url
        draw.text((12, 60), f"URL:  {url_display}",
                  font=font_body, fill=(140, 180, 255, 230))

        # Reason (first line, truncated)
        reason_line = reason.split("\n")[0][:200]
        draw.text((12, 82), f"Why:  {reason_line}",
                  font=font_body, fill=(255, 160, 80, 240))

        # Compose and save
        combined = Image.alpha_composite(img, overlay)
        combined.convert("RGB").save(out_path, "PNG")
        logger.info(f"[hitl] Annotated screenshot saved: {out_path}")
        return out_path

    except Exception as e:
        logger.warning(f"[hitl] Screenshot annotation failed: {e}")
        import traceback
        traceback.print_exc()
        return raw_path




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


# ---------------------------------------------------------------------------
# HITL Controller
# ---------------------------------------------------------------------------

class HITLController:
    """
    Manages the pause -> human-takes-control -> resume lifecycle.

    Key design: the Playwright Page object is kept alive throughout.
    The human operates the exact same browser instance -- same cookies,
    same session state, same filled forms.
    """

    def __init__(self, evidence_dir: str = "evidence", headless: bool = False):
        self.evidence_dir = evidence_dir
        # headless=False is critical for real HITL -- the window must be visible
        self.headless = headless
        self._intervention_counter = 0

    def _next_request_id(self) -> str:
        self._intervention_counter += 1
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"HITL_{ts}_{self._intervention_counter:03d}"

    def _take_screenshot(self, page: "Page", request_id: str) -> Optional[str]:
        """Capture a raw screenshot of the current browser state."""
        try:
            screenshot_dir = os.path.join(self.evidence_dir, "screenshots")
            os.makedirs(screenshot_dir, exist_ok=True)
            path = os.path.join(screenshot_dir, f"{request_id}_raw.png")
            page.screenshot(path=path, full_page=False)
            logger.info(f"[hitl] Raw screenshot saved: {path}")
            return path
        except Exception as e:
            logger.warning(f"[hitl] Could not take screenshot: {e}")
            return None

    def _annotate(
        self,
        raw_path: str,
        request_id: str,
        step_id: str,
        reason: str,
        current_url: str,
    ) -> str:
        """Annotate the raw screenshot and save as a separate annotated file."""
        screenshot_dir = os.path.join(self.evidence_dir, "screenshots")
        annotated_path = os.path.join(screenshot_dir, f"{request_id}_annotated.png")
        return annotate_screenshot(
            raw_path=raw_path,
            step_id=step_id,
            reason=reason,
            current_url=current_url,
            out_path=annotated_path,
        )

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

        Flow:
          1. Take + annotate screenshot (human sees exactly what's wrong)
          2. Save InterventionRequest JSON to disk
          3. Print clear terminal notice with full context
          4. If non_interactive (CI/test): auto-resolve immediately
          5. If interactive (real HITL): block on input() while human acts
             in the LIVE browser window, then resume

        Returns dict: { resolved, url_after, human_notes, duration_seconds }
        """
        request_id = self._next_request_id()
        current_url = page.url

        # -- Step 1: Screenshot + annotation -------------------------------
        raw_path = self._take_screenshot(page, request_id)
        annotated_path = None
        if raw_path:
            annotated_path = self._annotate(
                raw_path=raw_path,
                request_id=request_id,
                step_id=current_step_id,
                reason=reason,
                current_url=current_url,
            )

        # Use annotated path as the canonical screenshot reference
        screenshot_path = annotated_path or raw_path

        # -- Step 2: Build + persist intervention request -------------------
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
        interventions_dir = os.path.join(self.evidence_dir, "interventions")
        request_path = request.save(interventions_dir)
        logger.warning(f"[hitl] Intervention request saved: {request_path}")

        # -- Step 3: Non-interactive mode (CI / automated tests) ------------
        if non_interactive:
            logger.info("[hitl] Non-interactive mode: auto-resolving intervention")
            request.status = "resolved"
            request.human_notes = "Auto-resolved in non-interactive mode"
            request.save(interventions_dir)
            return {
                "resolved": True,
                "url_after": current_url,
                "human_notes": "auto-resolved",
                "duration_seconds": 0,
            }

        # -- Step 4: Real interactive HITL ---------------------------------
        #
        # At this point the browser window is open and VISIBLE (because the
        # ReplayEngine / DiscoveryAgent launched with headless=False when
        # hitl_enabled=True and a real operator is expected).
        #
        # We print a clear notice, then block on input().
        # The human looks at the browser, fixes whatever is wrong
        # (dismisses the dialog, approves the permission, handles the 2FA,
        # navigates past the problem), then presses Enter here.
        # Playwright's page object is still live -- we just continue from
        # wherever the human left it.

        print("\n")
        print("+" + "=" * 68 + "+")
        print("|  [!!]  HUMAN INTERVENTION REQUIRED" + " " * 35 + "|")
        print("+" + "=" * 68 + "+")
        print(f"|  Request ID  : {request_id:<52}|")
        print(f"|  Capability  : {capability_name:<52}|")
        print(f"|  Stuck step  : {current_step_id:<52}|")
        print(f"|  Severity    : {severity.upper():<52}|")
        print("+" + "=" * 68 + "+")
        # Word-wrap the reason to fit the box
        reason_lines = textwrap.wrap(f"Reason: {reason}", width=66)
        for line in reason_lines[:4]:            # max 4 lines
            print(f"|  {line:<66}|")
        print("+" + "=" * 68 + "+")
        print(f"|  URL         : {current_url[:52]:<52}|")
        if screenshot_path:
            short = os.path.basename(screenshot_path)
            print(f"|  Screenshot  : {short:<52}|")
        print("+" + "=" * 68 + "+")
        print("|                                                                    |")
        print("|  The browser window is OPEN and showing the stuck state.           |")
        print("|  Please:                                                           |")
        print("|    1. Look at the browser -- the annotated screenshot shows what    |")
        print("|       went wrong and at which step.                                |")
        print("|    2. Fix the issue manually in the browser (dismiss the dialog,   |")
        print("|       approve the permission, handle 2FA, etc.)                    |")
        print("|    3. Leave the browser on a page the automation can continue from.|")
        print("|    4. Come back here and press ENTER to hand control back.         |")
        print("|                                                                    |")
        print("+" + "=" * 68 + "+")
        print()

        start_time = time.time()
        request.status = "in_progress"
        request.save(interventions_dir)

        try:
            human_notes = input("  Your notes (optional, then press ENTER): ").strip()
        except (KeyboardInterrupt, EOFError):
            human_notes = "interrupted"

        duration = time.time() - start_time
        url_after = page.url

        # Update and persist resolved request
        request.status = "resolved"
        request.human_notes = human_notes
        request.save(interventions_dir)

        print(f"\n  [OK]  Control returned to automation.")
        print(f"    Human held control for {duration:.1f}s")
        print(f"    Resuming from: {url_after}\n")

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
        Heuristic: if the engine has failed the same step N times in a row,
        it's stuck. Escalate to human.
        """
        if consecutive_failures >= max_failures_before_escalation:
            logger.warning(
                f"[hitl] Stuck detected after {consecutive_failures} consecutive "
                f"failures. Last error: {last_error}"
            )
            return True
        return False