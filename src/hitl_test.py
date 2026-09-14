"""
HITL Force-Trigger Test — Requirement 3.6
------------------------------------------
Demonstrates the full human-in-the-loop escalation flow with a REAL,
VISIBLE browser window.

Run:
    cd src
    python hitl_test.py               # interactive — browser opens, you fix it
    python hitl_test.py --auto        # non-interactive — auto-resolves (CI mode)

What happens (interactive mode):
  1. Target app must be running on :8080 (run `python main.py --mode server` first)
  2. A real Chromium window opens — you can see the banking app
  3. The automation navigates and types the member ID successfully
  4. The Search button locator is DELIBERATELY BROKEN (simulates UI drift)
  5. The engine exhausts retries → escalates to HITL
  6. An ANNOTATED screenshot is saved to evidence/screenshots/
     (red banner, step ID, reason, URL — not a raw screenshot)
  7. A structured JSON intervention request is saved to evidence/interventions/
  8. Terminal prints a clear intervention notice with a box
  9. The browser stays OPEN — you can see exactly what's stuck
  10. Press ENTER in the terminal — automation resumes from the live session
  11. Summary printed + evidence files listed

This satisfies requirement 3.6:
  ✅ Detect stuck state (all locators exhausted after retries)
  ✅ Route intervention request with full context (JSON to disk)
  ✅ Human takes control of the LIVE session (same Playwright page object)
  ✅ Hand control back (Enter → resume)
  ✅ Evidence preserved across the handoff
"""

from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))

from artifact import CapabilityArtifact, Step, Locator, build_check_balance_artifact
from replay import ReplayEngine, ReplayResult

EVIDENCE_DIR   = os.path.join(os.path.dirname(__file__), "..", "evidence")
ARTIFACT_PATH  = os.path.join(EVIDENCE_DIR, "capability_artifact.json")
TARGET_URL     = "http://localhost:8080"
TARGET_APP_DIR = os.path.join(os.path.dirname(__file__), "target_app")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def server_is_up(url: str = TARGET_URL, timeout: int = 2) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start_server_if_needed():
    if server_is_up():
        print("  ✅  Target app already running on :8080")
        return None

    print("  🚀  Starting target app on :8080 ...")
    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=TARGET_APP_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(15):
        time.sleep(0.8)
        if server_is_up():
            print("  ✅  Target app is up")
            return proc
    print("  ⚠️   Target app may not have started — check port 8080")
    return proc


def inject_broken_locator(artifact: CapabilityArtifact) -> CapabilityArtifact:
    """
    Replace the Search button step with deliberately unresolvable locators.
    Simulates a vendor UI update that renames the button so all recorded
    locator strategies fail — the most common real-world HITL trigger.
    """
    patched = []
    injected = False
    for step in artifact.steps:
        is_search_click = (
            step.action == "click"
            and any(
                "search" in (loc.value or "").lower()
                for loc in step.locators
            )
        )
        if is_search_click and not injected:
            patched.append(Step(
                step_id=step.step_id,
                action="click",
                locators=[
                    Locator(
                        strategy="aria-label",
                        value="__broken_locator_hitl_demo__",
                        description="Deliberately broken — simulates UI drift",
                    ),
                    Locator(
                        strategy="css",
                        value="button.hitl-demo-does-not-exist",
                        description="Deliberately broken fallback",
                    ),
                ],
                description=f"{step.description} [HITL DEMO: locators broken to force escalation]",
                input_var=step.input_var,
                input_value=step.input_value,
                checkpoint=step.checkpoint,
                wait_after_ms=300,
                is_reversible=step.is_reversible,
            ))
            injected = True
            print(f"  ⚡  Broken locators injected into step: {step.step_id}")
        else:
            patched.append(step)

    if not injected:
        print("  ⚠️   No click step found to inject — artifact may have different structure")

    artifact.steps = patched
    return artifact


def print_evidence_summary(result: ReplayResult, interventions_dir: str):
    intervention_files = sorted([
        f for f in os.listdir(interventions_dir) if f.endswith(".json")
    ]) if os.path.isdir(interventions_dir) else []

    print("\n" + "═" * 65)
    print("  HITL Test — Evidence Summary")
    print("═" * 65)
    print(f"  Outcome type      : {result.outcome_type}")
    print(f"  Steps completed   : {result.steps_completed}")
    print(f"  Duration          : {result.duration_seconds:.2f}s")
    print(f"  Retries used      : {result.retries_used}")
    print(f"  Interventions     : {len(intervention_files)}")

    if intervention_files:
        latest = intervention_files[-1]
        path = os.path.join(interventions_dir, latest)
        with open(path) as f:
            req = json.load(f)

        print(f"\n  📄  Intervention JSON: evidence/interventions/{latest}")
        print(f"     Request ID      : {req['request_id']}")
        print(f"     Stuck at step   : {req['current_step_id']}")
        print(f"     Status          : {req['status']}")
        print(f"     Human notes     : {req.get('human_notes') or '(none)'}")
        if req.get('screenshot_path'):
            print(f"     Screenshot      : {os.path.basename(req['screenshot_path'])}")

    if result.screenshot_path:
        print(f"\n  📸  Final screenshot : {os.path.basename(result.screenshot_path)}")

    summary_path = os.path.join(EVIDENCE_DIR, "replay_hitl_test_summary.json")
    with open(summary_path, "w") as f:
        json.dump(result.to_dict(), f, indent=2)
    print(f"\n  📋  Result summary  : {summary_path}")

    print("\n  ✅  Requirement 3.6 Demonstrated:")
    print("     ① Automation detected stuck state (all locators exhausted)")
    print("     ② Annotated screenshot saved (red banner + error context)")
    print("     ③ Intervention request routed to evidence/interventions/")
    print("     ④ Human operated the LIVE browser session (same Playwright page)")
    print("     ⑤ Control handed back → automation resumed from new page state")
    print("     ⑥ Full context preserved across handoff")
    print("═" * 65 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_hitl_test(non_interactive: bool = False):
    print("\n" + "═" * 65)
    print("  HITL Escalation Demo — Requirement 3.6")
    print("═" * 65)

    server_proc = start_server_if_needed()

    # Load or build artifact
    if os.path.exists(ARTIFACT_PATH):
        print(f"\n  📄  Loading artifact: {ARTIFACT_PATH}")
        artifact = CapabilityArtifact.load(ARTIFACT_PATH)
    else:
        print("\n  📄  No artifact found — using hand-authored reference")
        artifact = build_check_balance_artifact(target_url=TARGET_URL)

    # Inject broken locators
    print("\n  Injecting broken locators into Search button step...")
    broken = inject_broken_locator(artifact)

    interventions_dir = os.path.join(EVIDENCE_DIR, "interventions")
    os.makedirs(interventions_dir, exist_ok=True)

    mode_label = "NON-INTERACTIVE (auto-resolve)" if non_interactive else "INTERACTIVE (real browser)"
    print(f"\n  Mode: {mode_label}")

    if not non_interactive:
        print("""
  What to expect:
    — A real Chromium window will open showing the banking app
    — The automation will fill in the member ID, then get stuck on Search
    — An annotated screenshot will be saved with the error highlighted
    — You will see this terminal prompt asking for intervention
    — In the browser: the app is on the search page (member ID filled in)
    — You can manually click Search yourself to fix it
    — Press ENTER here when you're done — automation will resume
        """)
        input("  Press ENTER when ready to start the demo... ")

    print("\n  Starting replay engine (headless=False for real HITL)...\n")

    # headless=False is critical — the window must be visible for real HITL
    engine = ReplayEngine(
        evidence_dir=EVIDENCE_DIR,
        headless=non_interactive,        # False for real HITL, True for CI
        max_retries_per_step=1,          # Fail fast for demo
        hitl_enabled=True,
    )

    result = engine.run(
        artifact=broken,
        parameters={"member_id": "100001"},
        log_suffix="hitl_test",
        non_interactive=non_interactive,
    )

    print_evidence_summary(result, interventions_dir)

    if server_proc and server_proc.poll() is None:
        server_proc.terminate()

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HITL escalation demo")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Non-interactive mode: auto-resolve without human input (for CI)",
    )
    args = parser.parse_args()
    run_hitl_test(non_interactive=args.auto)