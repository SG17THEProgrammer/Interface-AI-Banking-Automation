"""
Main Orchestrator — Single Entry Point
----------------------------------------
Run this file to execute the complete system:
  1. Start the mock target app (if not already running)
  2. Run LLM discovery → produces capability_artifact.json + discovery_run.log
  3. Run deterministic replay (success case) → replay_success.log
  4. Run deterministic replay (failure/business-outcome case) → replay_failure.log
  5. Print a summary report

Usage:
    python main.py --mode all                    # full end-to-end run
    python main.py --mode discovery              # only run discovery
    python main.py --mode replay                 # only run replay (needs artifact)
    python main.py --mode replay --member 999999 # replay with specific member
    python main.py --mode server                 # only start the target app
    python main.py --mode chat                   # start chat UI on :5000 + target app on :8080
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import subprocess
import sys
import time
import threading
from dotenv import load_dotenv

load_dotenv()

# ── Make src/ importable ──────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from artifact import CapabilityArtifact, build_check_balance_artifact
from discovery import DiscoveryAgent
from replay import ReplayEngine, ReplayResult

# ── Logging setup ─────────────────────────────────────────────────────────
_evidence_dir_for_log = os.path.join(os.path.dirname(__file__), "..", "evidence")
os.makedirs(_evidence_dir_for_log, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(_evidence_dir_for_log, "run.log"), mode="a"
        ),
    ],
)
logger = logging.getLogger("main")

# ── Health-check log filter (keeps run.log clean) ─────────────────────────
class _HealthCheckFilter(logging.Filter):
    """
    Drop Werkzeug access-log records for polling routes so that
    /health and /api/health-proxy GET 200s don't bloat run.log.
    """
    _SKIP = {"/health", "/api/health-proxy"}

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(skip in msg for skip in self._SKIP)

logging.getLogger("werkzeug").addFilter(_HealthCheckFilter())

# ── Config ────────────────────────────────────────────────────────────────
TARGET_URL     = "http://localhost:8080"
EVIDENCE_DIR   = os.path.join(os.path.dirname(__file__), "..", "evidence")
ARTIFACT_PATH  = os.path.join(EVIDENCE_DIR, "capability_artifact.json")
DEFAULT_MEMBER = "100001"   # Alice Johnson — happy path
MISSING_MEMBER = "000000"   # Does not exist — business outcome test
TARGET_APP_DIR = os.path.join(os.path.dirname(__file__), "target_app")
UI_DIR         = os.path.join(os.path.dirname(__file__), "ui")


# ── Helpers ───────────────────────────────────────────────────────────────

def ensure_evidence_dir():
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    os.makedirs(os.path.join(EVIDENCE_DIR, "screenshots"), exist_ok=True)
    os.makedirs(os.path.join(EVIDENCE_DIR, "jobs"), exist_ok=True)


def print_banner(text: str):
    width = 70
    print("\n" + "═" * width)
    print(f"  {text}")
    print("═" * width)


def print_result(label: str, result: ReplayResult):
    icon = "✅" if result.success else ("⚑ " if result.outcome_type == "business_outcome" else "❌")
    print(f"\n{icon}  {label}")
    print(f"   Outcome type  : {result.outcome_type}")
    print(f"   Steps done    : {result.steps_completed}")
    print(f"   Duration      : {result.duration_seconds:.2f}s")
    print(f"   Retries used  : {result.retries_used}")
    if result.outputs:
        print(f"   Outputs       :")
        for k, v in result.outputs.items():
            print(f"     {k}: {v}")
    if result.business_outcome:
        print(f"   Business msg  : {result.business_outcome}")
    if result.error:
        print(f"   Error         : {result.error}")
    if result.failed_step_id:
        print(f"   Failed step   : {result.failed_step_id}")
    if result.screenshot_path:
        print(f"   Screenshot    : {result.screenshot_path}")


def wait_for_server(url: str, timeout: int = 15) -> bool:
    """Poll the target app's /health endpoint until it responds."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def start_target_app() -> subprocess.Popen:
    """Start the Flask mock app in a subprocess."""
    print_banner("Starting mock banking app on http://localhost:8080")
    env = os.environ.copy()
    env["FLASK_ENV"] = "production"
    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=TARGET_APP_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if wait_for_server(TARGET_URL, timeout=12):
        print("  ✅  Mock app is up at http://localhost:8080")
    else:
        print("  ⚠️   Mock app may not have started. Check that port 8080 is free.")
    return proc


def start_chat_ui() -> subprocess.Popen:
    """Start the Flask chat UI in a subprocess."""
    print_banner("Starting Chat UI on http://localhost:5000")
    env = os.environ.copy()
    env["FLASK_ENV"] = "production"
    chat_app_path = os.path.join(UI_DIR, "chat_app.py")
    proc = subprocess.Popen(
        [sys.executable, chat_app_path],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if wait_for_server("http://localhost:5000", timeout=10):
        print("  ✅  Chat UI is up at http://localhost:5000")
    else:
        print("  ⚠️   Chat UI may not have started. Check that port 5000 is free.")
    return proc


# ── Phase runners ─────────────────────────────────────────────────────────

def run_discovery(api_key: str, headless: bool = True) -> CapabilityArtifact:
    print_banner("Phase 1 — LLM Discovery Agent")
    print("  Goal: 'Look up member 100001 and retrieve their current balance'")
    print("  This will take 30–90 seconds as the LLM drives the browser...\n")

    agent = DiscoveryAgent(
        api_key=api_key,
        evidence_dir=EVIDENCE_DIR,
        headless=headless,
        max_steps=10,
    )

    result = agent.run(
        goal=(
            "Navigate to the member search page, search for member ID '100001', "
            "and extract their current balance and account status. "
            "If the member is not found, record that as a business outcome."
        ),
        target_url=TARGET_URL,
        capability_name="check_member_balance",
        runtime_params={"member_id": DEFAULT_MEMBER},
    )

    if result["success"]:
        print(f"\n  ✅  Discovery complete. {result['steps_taken']} steps taken.")
        print(f"  📄  Artifact saved: {result['artifact_path']}")
        print(f"  📋  Run log saved : {result['log_path']}")
    else:
        print(f"\n  ⚠️   Discovery did not confirm goal complete after {result['steps_taken']} steps.")
        print(f"  📄  Partial artifact saved: {result['artifact_path']}")

    return result["artifact"]


def run_replay_success(artifact: CapabilityArtifact, headless: bool = True) -> ReplayResult:
    print_banner("Phase 2a — Deterministic Replay (Success Case)")
    print(f"  Member: {DEFAULT_MEMBER} (Alice Johnson — active account)")
    print("  Expected: outputs contain balance and account_status\n")

    engine = ReplayEngine(
        evidence_dir=EVIDENCE_DIR,
        headless=headless,
        hitl_enabled=True,
    )
    result = engine.run(
        artifact=artifact,
        parameters={"member_id": DEFAULT_MEMBER},
        log_suffix="success",
        non_interactive=True,
    )
    print_result("Replay — success case", result)

    summary_path = os.path.join(EVIDENCE_DIR, "replay_success_summary.json")
    with open(summary_path, "w") as f:
        json.dump(result.to_dict(), f, indent=2)
    print(f"  📋  Summary saved : {summary_path}")

    return result


def run_replay_failure(artifact: CapabilityArtifact, headless: bool = True) -> ReplayResult:
    print_banner("Phase 2b — Deterministic Replay (Business Outcome / Failure Case)")
    print(f"  Member: {MISSING_MEMBER} (does not exist — should be a business outcome)")
    print("  Expected: outcome_type='business_outcome', NOT a crash\n")

    engine = ReplayEngine(
        evidence_dir=EVIDENCE_DIR,
        headless=headless,
        hitl_enabled=True,
    )
    result = engine.run(
        artifact=artifact,
        parameters={"member_id": MISSING_MEMBER},
        log_suffix="failure",
        non_interactive=True,
    )
    print_result("Replay — failure/business-outcome case", result)

    summary_path = os.path.join(EVIDENCE_DIR, "replay_business_outcome_summary.json")
    with open(summary_path, "w") as f:
        json.dump(result.to_dict(), f, indent=2)
    print(f"  📋  Summary saved : {summary_path}")

    return result


def run_replay_frozen_account(artifact: CapabilityArtifact, headless: bool = True) -> ReplayResult:
    """Bonus test: frozen account — also a business outcome variant."""
    print_banner("Phase 2c — Replay (Frozen Account Case)")
    print("  Member: 100003 (Carol Williams — frozen account)")

    engine = ReplayEngine(evidence_dir=EVIDENCE_DIR, headless=headless)
    result = engine.run(
        artifact=artifact,
        parameters={"member_id": "100003"},
        log_suffix="frozen",
        non_interactive=True,
    )
    print_result("Replay — frozen account", result)
    return result


# ── Argument parser ───────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="interface.ai Computer-Use Automation System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --mode all                     # Full end-to-end run
  python main.py --mode discovery               # Discovery only
  python main.py --mode replay                  # Replay only (uses saved artifact)
  python main.py --mode replay --member 100002  # Replay for a specific member
  python main.py --mode server                  # Start target app only
  python main.py --mode chat                    # Start chat UI + target app
  python main.py --mode all --visible           # Run with visible browser window
        """,
    )
    p.add_argument(
        "--mode",
        choices=["all", "discovery", "replay", "server", "chat"],
        default="all",
        help="Which phase to run (default: all)",
    )
    p.add_argument(
        "--member",
        default=DEFAULT_MEMBER,
        help=f"Member ID for replay (default: {DEFAULT_MEMBER})",
    )
    p.add_argument(
        "--visible",
        action="store_true",
        help="Show the browser window (default: headless)",
    )
    p.add_argument(
        "--skip-server",
        action="store_true",
        help="Don't start the target app (assumes it's already running)",
    )
    p.add_argument(
        "--use-prebuilt-artifact",
        action="store_true",
        help="Use the hand-authored artifact instead of running discovery",
    )
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    ensure_evidence_dir()

    # Get API key
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key and args.mode in ("all", "discovery"):
        print("\n❌  GROQ_API_KEY environment variable not set.")
        print("   Set it with:  export GROQ_API_KEY=gsk_...")
        print("   Or run replay-only with:  python main.py --mode replay --use-prebuilt-artifact")
        print("   Or start the chat UI with: python main.py --mode chat")
        sys.exit(1)

    headless = not args.visible
    server_proc = None
    chat_proc   = None

    try:
        # ── Chat mode ─────────────────────────────────────────────────────
        if args.mode == "chat":
            if not args.skip_server:
                server_proc = start_target_app()
                time.sleep(1)
            chat_proc = start_chat_ui()
            print("\n  Both servers running.")
            print("  Chat UI   → http://localhost:5000")
            print("  Banking   → http://localhost:8080")
            print("  Dashboard → http://localhost:5000/dashboard")
            print("\n  Press Ctrl+C to stop.\n")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            return

        # ── Target app only ───────────────────────────────────────────────
        if args.mode == "server":
            proc = start_target_app()
            print("\n  Server running. Press Ctrl+C to stop.")
            proc.wait()
            return

        if not args.skip_server:
            server_proc = start_target_app()
            time.sleep(1)

        # ── Determine which artifact to use ──────────────────────────────
        artifact = None

        if args.mode in ("all", "discovery") and not args.use_prebuilt_artifact:
            artifact = run_discovery(api_key=api_key, headless=headless)

        elif args.use_prebuilt_artifact or args.mode == "replay":
            if os.path.exists(ARTIFACT_PATH):
                print(f"\n  📄  Loading existing artifact: {ARTIFACT_PATH}")
                artifact = CapabilityArtifact.load(ARTIFACT_PATH)
            else:
                print("\n  📄  No saved artifact found. Using hand-authored reference artifact.")
                artifact = build_check_balance_artifact(target_url=TARGET_URL)
                artifact.save(ARTIFACT_PATH)
                print(f"  📄  Reference artifact saved: {ARTIFACT_PATH}")

        # ── Run replay ───────────────────────────────────────────────────
        if args.mode in ("all", "replay"):
            if args.mode == "replay" and args.member != DEFAULT_MEMBER:
                engine = ReplayEngine(evidence_dir=EVIDENCE_DIR, headless=headless)
                result = engine.run(
                    artifact=artifact,
                    parameters={"member_id": args.member},
                    log_suffix=f"member_{args.member}",
                    non_interactive=True,
                )
                print_result(f"Replay for member {args.member}", result)
            else:
                run_replay_success(artifact, headless=headless)
                run_replay_failure(artifact, headless=headless)
                if args.mode == "all":
                    run_replay_frozen_account(artifact, headless=headless)

        # ── Final summary ────────────────────────────────────────────────
        print_banner("Run Complete — Evidence Files")
        evidence_files = []
        for root, _, files in os.walk(EVIDENCE_DIR):
            for f in sorted(files):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, EVIDENCE_DIR)
                size = os.path.getsize(full)
                evidence_files.append((rel, size))

        for rel, size in sorted(evidence_files):
            print(f"  📄  evidence/{rel}  ({size:,} bytes)")

        print(f"\n  All evidence saved in: {os.path.abspath(EVIDENCE_DIR)}")

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    finally:
        if chat_proc and chat_proc.poll() is None:
            chat_proc.terminate()
            chat_proc.wait()
            print("  Chat UI stopped.")
        if server_proc and server_proc.poll() is None:
            server_proc.terminate()
            server_proc.wait()
            print("  Target app stopped.")


if __name__ == "__main__":
    main()