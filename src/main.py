"""
main.py — Single entry point.

Modes:
  chat          Start banking app (8080) + chat UI (5000). Open http://localhost:5000
  replay        Run deterministic engine for one member (no LLM, no UI).
  demo          Run the full automated test suite and print results.
  server        Start only the banking app on port 8080.
  hitl-test     Force a HITL escalation to demonstrate the flow.

Usage:
  python main.py                          # chat mode (default)
  python main.py --mode replay --member 100002
  python main.py --mode demo
  python main.py --mode server
  python main.py --mode hitl-test --auto
"""

from __future__ import annotations
import argparse
import importlib.util
import json
import logging
import os
import socket
import sys
import threading
import time

# ── Paths ──────────────────────────────────────────────────────────────────
_SRC           = os.path.dirname(os.path.abspath(__file__))
TARGET_APP_DIR = os.path.join(_SRC, "target_app")
UI_DIR         = os.path.join(_SRC, "ui")
EVIDENCE_DIR   = os.path.join(_SRC, "..", "evidence")
TARGET_URL     = "http://localhost:8080"

# ── sys.path ───────────────────────────────────────────────────────────────
# Both _SRC and TARGET_APP_DIR must be on the path before any local imports.
for _p in [_SRC, TARGET_APP_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Local imports (after path setup) ──────────────────────────────────────
import capabilities.check_balance as cap_check_balance
import capabilities.update_status as cap_update_status
from engine.engine import ReplayEngine, ReplayResult

# ── Evidence dirs ──────────────────────────────────────────────────────────
for _d in [EVIDENCE_DIR,
           os.path.join(EVIDENCE_DIR, "screenshots"),
           os.path.join(EVIDENCE_DIR, "interventions")]:
    os.makedirs(_d, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(EVIDENCE_DIR, "run.log"), mode="a"),
    ],
)
logger = logging.getLogger("main")

# Suppress noisy health-check lines from Werkzeug access log
class _HealthFilter(logging.Filter):
    def filter(self, record):
        m = record.getMessage()
        return "/health" not in m and "/api/health-proxy" not in m

logging.getLogger("werkzeug").addFilter(_HealthFilter())


# ══════════════════════════════════════════════════════════════════════════
# Server helpers
# ══════════════════════════════════════════════════════════════════════════

def _is_port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    result = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    return result


def _load_flask_app(module_name: str, file_path: str, extra_paths: list[str] = None):
    """
    Load a Flask app from an absolute file path via importlib.
    Inserts extra_paths into sys.path first so relative imports inside
    the module (e.g. 'from database import ...') resolve correctly.
    """
    for p in (extra_paths or []):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _start_banking_app():
    """Load and run the banking Flask app in this thread (call from a daemon thread)."""
    mod = _load_flask_app(
        "bank_app",
        os.path.join(TARGET_APP_DIR, "app.py"),
        extra_paths=[TARGET_APP_DIR],   # so 'from database import ...' works
    )
    mod.app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)


def _start_chat_app():
    """Load and run the chat Flask app in this thread (call from a daemon thread)."""
    mod = _load_flask_app(
        "chat_app",
        os.path.join(UI_DIR, "chat_app.py"),
        extra_paths=[_SRC],             # so 'from capabilities import ...' works
    )
    mod.app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)


def _ensure_server(port: int, target_fn, label: str, wait_s: int = 12):
    """Start target_fn in a daemon thread if port isn't already open."""
    if _is_port_open(port):
        print(f"  ✅  {label} already running on port {port}")
        return
    print(f"  ⏳  Starting {label} on port {port} …")
    t = threading.Thread(target=target_fn, daemon=True, name=label)
    t.start()
    for _ in range(wait_s * 2):
        time.sleep(0.5)
        if _is_port_open(port):
            print(f"  ✅  {label} ready")
            return
    print(f"  ⚠️   {label} did not start on port {port}. Is the port free?")


# ══════════════════════════════════════════════════════════════════════════
# Shared utilities
# ══════════════════════════════════════════════════════════════════════════

def _patch_update_navigate(artifact, member_id: str):
    for step in artifact.steps:
        if step.action == "navigate" and step.input_value and "{member_id}" in step.input_value:
            step.input_value = step.input_value.replace("{member_id}", member_id)
            step.input_var   = None
            break


def sep(title: str = ""):
    w = 68
    print("\n" + "─" * w)
    if title:
        print(f"  {title}")
        print("─" * w)


def print_result(label: str, result: ReplayResult):
    icons = {"success": "✅", "business_outcome": "⚑ ", "hard_failure": "❌"}
    icon  = icons.get(result.outcome_type, "?")
    print(f"\n{icon}  {label}")
    print(f"   Outcome  : {result.outcome_type}")
    print(f"   Steps    : {result.steps_completed}   "
          f"Duration: {result.duration_seconds:.1f}s   "
          f"Retries: {result.retries_used}")
    for k, v in (result.outputs or {}).items():
        print(f"   {k}: {v}")
    if result.business_outcome:
        print(f"   Message  : {result.business_outcome[:120]}")
    if result.error:
        print(f"   Error    : {result.error[:120]}")
    if result.failed_step_id:
        print(f"   At step  : {result.failed_step_id}")


def _list_evidence():
    for root, _, files in os.walk(EVIDENCE_DIR):
        for f in sorted(files):
            full = os.path.join(root, f)
            rel  = os.path.relpath(full, EVIDENCE_DIR)
            size = os.path.getsize(full)
            print(f"  📄  evidence/{rel}  ({size:,} bytes)")


# ══════════════════════════════════════════════════════════════════════════
# Modes
# ══════════════════════════════════════════════════════════════════════════

def mode_chat(args):
    sep("MemberLink Automation System — Chat Mode")

    _ensure_server(8080, _start_banking_app, "Banking app")
    _ensure_server(5000, _start_chat_app,    "Chat UI")

    print()
    print("  ┌─────────────────────────────────────────────┐")
    print("  │  Open  http://localhost:5000  in Chrome     │")
    print("  └─────────────────────────────────────────────┘")
    print()
    print("  Banking app : http://localhost:8080")
    print("  Chat UI     : http://localhost:5000")
    print("  Press Ctrl+C to stop.\n")

    try:
        import webbrowser
        time.sleep(0.5)
        webbrowser.open("http://localhost:5000")
    except Exception:
        pass

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Shutting down. Goodbye.")


def mode_replay(args):
    _ensure_server(8080, _start_banking_app, "Banking app")
    time.sleep(1)

    sep(f"Replay — balance lookup for member {args.member}")
    artifact = cap_check_balance.build(TARGET_URL)
    engine   = ReplayEngine(evidence_dir=EVIDENCE_DIR, headless=True)
    result   = engine.run(artifact, {"member_id": args.member},
                          log_suffix=f"member_{args.member}")
    print_result("Balance lookup", result)

    path = os.path.join(EVIDENCE_DIR, f"replay_{args.member}_summary.json")
    with open(path, "w") as f:
        json.dump(result.to_dict(), f, indent=2)
    print(f"\n  Summary → {path}")


def mode_demo(args):
    _ensure_server(8080, _start_banking_app, "Banking app")
    time.sleep(1)

    engine = ReplayEngine(evidence_dir=EVIDENCE_DIR, headless=True)

    sep("Demo 1 — Balance lookup (success): Alice / 100001")
    art1 = cap_check_balance.build(TARGET_URL)
    r1   = engine.run(art1, {"member_id": "100001"}, log_suffix="demo_balance_ok")
    print_result("Balance lookup — Alice", r1)

    sep("Demo 2 — Balance lookup (not found): 000000")
    r2 = engine.run(art1, {"member_id": "000000"}, log_suffix="demo_balance_nf")
    print_result("Balance lookup — not found", r2)

    sep("Demo 3 — Account update: Freeze Carol / 100003")
    art3 = cap_update_status.build(TARGET_URL)
    _patch_update_navigate(art3, "100003")
    r3 = engine.run(art3,
                    {"member_id": "100003", "new_status": "Frozen",
                     "reason_code": "DEMO-FREEZE"},
                    log_suffix="demo_update_carol")
    print_result("Update status — Carol", r3)

    sep("Demo 4 — Verify persistence: re-read Carol")
    art4 = cap_check_balance.build(TARGET_URL)
    r4   = engine.run(art4, {"member_id": "100003"}, log_suffix="demo_verify_carol")
    print_result("Verify Carol post-update", r4)

    sep("Demo complete — evidence files")
    _list_evidence()


def mode_server(args):
    sep("Banking app only — http://localhost:8080")
    _ensure_server(8080, _start_banking_app, "Banking app")
    print("  Press Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Stopped.")


def mode_hitl_test(args):
    from artifact import Step, Locator

    _ensure_server(8080, _start_banking_app, "Banking app")
    time.sleep(1)

    sep("HITL Escalation Demo")
    print("  Injecting broken locators into Search button step…")

    artifact = cap_check_balance.build(TARGET_URL)
    for i, step in enumerate(artifact.steps):
        if step.action == "click":
            artifact.steps[i] = Step(
                step_id=step.step_id,
                action="click",
                locators=[
                    Locator("css",        "button.__hitl_broken__",    "Deliberately broken"),
                    Locator("aria-label", "__hitl_broken_fallback__",  "Broken fallback"),
                ],
                description=step.description + " [HITL TEST: broken locators]",
                checkpoint=step.checkpoint,
                wait_after_ms=300,
                is_reversible=step.is_reversible,
            )
            print(f"  ✅  Broken locators injected into: {step.step_id}")
            break

    headless = args.auto   # False by default → browser opens
    engine   = ReplayEngine(
        evidence_dir=EVIDENCE_DIR,
        headless=headless,
        max_retries_per_step=1,
        hitl_enabled=True,
    )

    if not args.auto:
        print()
        print("  What will happen:")
        print("  1. Chromium opens — you'll see the banking app search page")
        print("  2. The bot types member ID 100001 successfully")
        print("  3. The bot gets STUCK trying to click Search (broken locators)")
        print("  4. This terminal shows INTERVENTION REQUIRED")
        print("  5. You manually click Search in the browser")
        print("  6. Press ENTER here — bot resumes and finishes")
        print()
        input("  Press ENTER to start… ")

    result = engine.run(
        artifact=artifact,
        parameters={"member_id": "100001"},
        log_suffix="hitl_demo",
        non_interactive=args.auto,
    )
    print_result("HITL demo", result)

    iv_dir = os.path.join(EVIDENCE_DIR, "interventions")
    if os.path.isdir(iv_dir):
        files = sorted(f for f in os.listdir(iv_dir) if f.endswith(".json"))
        if files:
            with open(os.path.join(iv_dir, files[-1])) as f:
                iv = json.load(f)
            print(f"\n  Intervention: evidence/interventions/{files[-1]}")
            print(f"    Stuck at  : {iv['current_step_id']}")
            print(f"    Status    : {iv['status']}")
            if iv.get("screenshot_path"):
                print(f"    Screenshot: {os.path.basename(iv['screenshot_path'])}")


# ══════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(
        description="MemberLink Automation System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                             # Start chat UI at http://localhost:5000
  python main.py --mode replay --member 100002
  python main.py --mode demo
  python main.py --mode server
  python main.py --mode hitl-test --auto
  python main.py --mode hitl-test            # browser opens for real HITL
        """,
    )
    p.add_argument("--mode",
                   choices=["chat", "replay", "demo", "server", "hitl-test"],
                   default="chat")
    p.add_argument("--member", default="100001")
    p.add_argument("--auto", action="store_true",
                   help="Auto-resolve HITL without human input (headless)")
    return p.parse_args()


def main():
    args = parse_args()
    {
        "chat":      mode_chat,
        "replay":    mode_replay,
        "demo":      mode_demo,
        "server":    mode_server,
        "hitl-test": mode_hitl_test,
    }[args.mode](args)


if __name__ == "__main__":
    main()