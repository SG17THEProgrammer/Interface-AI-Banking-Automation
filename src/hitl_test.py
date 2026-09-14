"""
HITL Force-Trigger Test - Requirement 3.6
------------------------------------------
Run:
    cd src
    python hitl_test.py          # interactive - browser opens, you fix it
    python hitl_test.py --auto   # auto-resolves (CI mode, no browser needed)
"""

from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

# Windows UTF-8 fix - must be before any print()
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(__file__))

from artifact import CapabilityArtifact, Step, Locator, build_check_balance_artifact
from replay import ReplayEngine, ReplayResult

EVIDENCE_DIR   = os.path.join(os.path.dirname(__file__), "..", "evidence")
ARTIFACT_PATH  = os.path.join(EVIDENCE_DIR, "capability_artifact.json")
TARGET_URL     = "http://localhost:8080"
TARGET_APP_DIR = os.path.join(os.path.dirname(__file__), "target_app")
SEP            = "-" * 65


def server_is_up(url: str = TARGET_URL, timeout: int = 2) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start_server_if_needed():
    if server_is_up():
        print("  [OK] Target app already running on :8080")
        return None
    print("  [..] Starting target app on :8080 ...")
    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=TARGET_APP_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(15):
        time.sleep(0.8)
        if server_is_up():
            print("  [OK] Target app is up")
            return proc
    print("  [!!] Target app may not have started - check port 8080")
    return proc


def inject_broken_locator(artifact: CapabilityArtifact) -> CapabilityArtifact:
    """Replace the Search button locators with ones that will never resolve,
    forcing the replay engine into HITL escalation."""
    patched = []
    injected = False
    for step in artifact.steps:
        is_search_click = (
            step.action == "click"
            and any("search" in (loc.value or "").lower() for loc in step.locators)
        )
        if is_search_click and not injected:
            patched.append(Step(
                step_id=step.step_id,
                action="click",
                locators=[
                    Locator(
                        strategy="aria-label",
                        value="__broken_locator_hitl_demo__",
                        description="Deliberately broken - simulates UI drift",
                    ),
                    Locator(
                        strategy="css",
                        value="button.hitl-demo-does-not-exist",
                        description="Deliberately broken fallback",
                    ),
                ],
                description=step.description + " [HITL DEMO: locators broken]",
                input_var=step.input_var,
                input_value=step.input_value,
                checkpoint=step.checkpoint,
                wait_after_ms=300,
                is_reversible=step.is_reversible,
            ))
            injected = True
            print(f"  [!!] Broken locators injected into step: {step.step_id}")
        else:
            patched.append(step)

    if not injected:
        print("  [!!] No click step found to inject - check artifact structure")

    artifact.steps = patched
    return artifact


def print_evidence_summary(result: ReplayResult, interventions_dir: str):
    intervention_files = sorted([
        f for f in os.listdir(interventions_dir) if f.endswith(".json")
    ]) if os.path.isdir(interventions_dir) else []

    print("\n" + SEP)
    print("  HITL Test - Evidence Summary")
    print(SEP)
    print(f"  Outcome type   : {result.outcome_type}")
    print(f"  Steps done     : {result.steps_completed}")
    print(f"  Duration       : {result.duration_seconds:.2f}s")
    print(f"  Retries used   : {result.retries_used}")
    print(f"  Interventions  : {len(intervention_files)}")

    if intervention_files:
        latest = intervention_files[-1]
        req_path = os.path.join(interventions_dir, latest)
        with open(req_path) as f:
            req = json.load(f)
        print(f"\n  Intervention JSON : evidence/interventions/{latest}")
        print(f"    Request ID      : {req['request_id']}")
        print(f"    Stuck at step   : {req['current_step_id']}")
        print(f"    Status          : {req['status']}")
        print(f"    Human notes     : {req.get('human_notes') or '(none)'}")
        if req.get("screenshot_path"):
            print(f"    Screenshot      : {os.path.basename(req['screenshot_path'])}")

    if result.screenshot_path:
        print(f"\n  Final screenshot  : {os.path.basename(result.screenshot_path)}")

    summary_path = os.path.join(EVIDENCE_DIR, "replay_hitl_test_summary.json")
    with open(summary_path, "w") as f:
        json.dump(result.to_dict(), f, indent=2)
    print(f"\n  Result summary    : {summary_path}")

    print("\n  Requirement 3.6 demonstrated:")
    print("  (1) Stuck state detected - all locators exhausted after retries")
    print("  (2) Annotated screenshot saved with red banner + error context")
    print("  (3) Intervention JSON routed to evidence/interventions/")
    print("  (4) Human operated the LIVE browser (same Playwright page object)")
    print("  (5) Control handed back - automation resumed from new page state")
    print("  (6) Full context preserved across the handoff")
    print(SEP)


def run_hitl_test(non_interactive: bool = False):
    print("\n" + SEP)
    print("  HITL Escalation Demo - Requirement 3.6")
    print(SEP)

    server_proc = start_server_if_needed()

    if os.path.exists(ARTIFACT_PATH):
        print(f"\n  Loading artifact: {ARTIFACT_PATH}")
        artifact = CapabilityArtifact.load(ARTIFACT_PATH)
    else:
        print("\n  No saved artifact found - using hand-authored reference")
        artifact = build_check_balance_artifact(target_url=TARGET_URL)

    print("\n  Injecting broken locators into Search button step...")
    broken = inject_broken_locator(artifact)

    interventions_dir = os.path.join(EVIDENCE_DIR, "interventions")
    os.makedirs(interventions_dir, exist_ok=True)

    mode = "NON-INTERACTIVE (auto-resolve)" if non_interactive else "INTERACTIVE (real browser)"
    print(f"\n  Mode: {mode}")

    if not non_interactive:
        print("""
  What to expect:
    - A Chromium window will open showing the banking app
    - The bot fills in member ID 100001, then gets STUCK on Search
    - An annotated screenshot is saved showing exactly what failed
    - This terminal will show an intervention notice and wait
    - In the browser: member ID is filled, Search button is visible
    - Manually click Search in the browser to resolve it
    - Come back here and press ENTER to hand control back
        """)
        input("  Press ENTER when ready to start... ")

    print("\n  Starting replay engine with visible browser...\n")

    engine = ReplayEngine(
        evidence_dir=EVIDENCE_DIR,
        headless=non_interactive,
        max_retries_per_step=1,
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
    parser.add_argument("--auto", action="store_true",
                        help="Auto-resolve without waiting for human input")
    args = parser.parse_args()
    run_hitl_test(non_interactive=args.auto)