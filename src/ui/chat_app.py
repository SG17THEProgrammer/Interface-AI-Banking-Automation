"""
ui/chat_app.py
--------------
User-facing chat server (port 5000).
Receives natural-language messages, routes them to capabilities,
runs the replay engine in a background thread, and returns results.
"""

from __future__ import annotations
import json
import logging
import os
import sys
import threading
import time
import uuid
import urllib.request

from flask import Flask, render_template, request, jsonify

_UI_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC    = os.path.dirname(_UI_DIR)
sys.path.insert(0, _SRC)

from capabilities.registry import match, list_capabilities
from engine.engine import ReplayEngine

logger = logging.getLogger(__name__)
app    = Flask(__name__, template_folder=os.path.join(_UI_DIR, "templates"))

EVIDENCE_DIR  = os.path.join(_SRC, "..", "evidence")
BANKING_URL   = "http://localhost:8080"

# job_id → result dict
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


# ── Pages ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("chat.html", capabilities=list_capabilities())


# ── Chat API ──────────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    data    = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "empty message"}), 400

    job_id = str(uuid.uuid4())
    cap    = match(message)

    # No capability matched
    if cap is None:
        return jsonify({
            "job_id": job_id,
            "type":   "no_match",
            "reply": (
                "I didn't understand that. I can help you:\n"
                "• **Check a balance** — e.g. 'What is Alice's balance?' or 'check member 100002'\n"
                "• **Update account status** — e.g. 'freeze Carol's account' or 'close account 100005'\n\n"
                "Just tell me which member and what you'd like to do."
            ),
        })

    # Missing required parameters
    if cap.missing:
        prompts = {
            "member_id":  "Which member? Use their name (Alice, Bob…) or their 6-digit ID.",
            "new_status": "What should the new status be? (Active / Frozen / Closed)",
        }
        asks = [prompts.get(k, f"Please provide: {k}") for k in cap.missing]
        return jsonify({
            "job_id":  job_id,
            "type":    "needs_info",
            "reply":   "\n".join(asks),
            "missing": cap.missing,
        })

    # Build the human-readable subject label (name preferred over ID)
    subject_label = _subject_label(cap.parameters)

    # Already-frozen / already-closed guard (instant check before running engine)
    if cap.name == "update_account_status":
        warning = _check_already_same_status(cap.parameters)
        if warning:
            return jsonify({
                "job_id":  job_id,
                "type":    "warning",
                "reply":   f"No update needed for {subject_label}.",
                "warning": warning,
            })

    # Kick off engine in background
    with _lock:
        _jobs[job_id] = {"status": "running", "capability": cap.name, "started": time.time()}

    threading.Thread(
        target=_run,
        args=(job_id, cap, subject_label),
        daemon=True,
    ).start()

    return jsonify({
        "job_id":        job_id,
        "type":          "accepted",
        "capability":    cap.name,
        "parameters":    cap.parameters,
        "subject_label": subject_label,
    })


@app.route("/api/job/<job_id>")
def job_status(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job)


# ── Proxy endpoints (avoids CORS issues from the browser) ─────────────────

@app.route("/api/audit-log")
def audit_log():
    """Proxy to banking app — avoids CORS issue when browser calls it directly."""
    try:
        with urllib.request.urlopen(f"{BANKING_URL}/api/audit-log", timeout=3) as r:
            return app.response_class(r.read(), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/health-proxy")
def health_proxy():
    """Proxy health check so the browser doesn't hit localhost:8080 directly."""
    try:
        with urllib.request.urlopen(f"{BANKING_URL}/health", timeout=2) as r:
            return app.response_class(r.read(), mimetype="application/json")
    except Exception:
        return jsonify({"status": "offline"}), 502


# ── Background worker ─────────────────────────────────────────────────────

def _run(job_id: str, cap, subject_label: str):
    try:
        engine   = ReplayEngine(
            evidence_dir=EVIDENCE_DIR,
            headless=True,
            max_retries_per_step=2,
            hitl_enabled=False,
        )
        artifact = cap.artifact
        params   = cap.parameters

        if artifact.name == "update_account_status":
            _patch_update_url(artifact, params)

        result = engine.run(
            artifact=artifact,
            parameters=params,
            log_suffix=f"chat_{job_id[:8]}",
            non_interactive=True,
        )

        with _lock:
            _jobs[job_id] = {
                "status":   "done",
                "outcome":  result.outcome_type,
                "success":  result.success,
                "outputs":  result.outputs,
                "business_outcome": result.business_outcome,
                "error":    result.error,
                "duration": round(result.duration_seconds, 2),
                "steps":    result.steps_completed,
                "reply":    _format_reply(cap.name, result, params, subject_label),
            }
    except Exception as e:
        logger.exception(f"[chat] Job {job_id} failed")
        with _lock:
            _jobs[job_id] = {
                "status":  "done",
                "outcome": "hard_failure",
                "success": False,
                "error":   str(e),
                "reply":   f"❌ Automation error: {e}",
            }


# ── Helpers ───────────────────────────────────────────────────────────────

def _subject_label(params: dict) -> str:
    """Return a human-friendly label: prefer resolved name over raw ID."""
    # Try to get the actual name from the banking app
    mid = params.get("member_id", "")
    try:
        with urllib.request.urlopen(f"{BANKING_URL}/api/member/{mid}", timeout=2) as r:
            data = json.loads(r.read())
            name = data.get("name", "")
            if name:
                return f"{name} (ID {mid})"
    except Exception:
        pass
    return f"member {mid}"


def _check_already_same_status(params: dict) -> str | None:
    """
    If the requested new_status matches the current status, return a warning string.
    Returns None if the update should proceed.
    """
    mid        = params.get("member_id", "")
    new_status = params.get("new_status", "")
    if not mid or not new_status:
        return None
    try:
        with urllib.request.urlopen(f"{BANKING_URL}/api/member/{mid}", timeout=2) as r:
            data = json.loads(r.read())
        current = data.get("account_status", "")
        name    = data.get("name", f"Member {mid}")
        if current.lower() == new_status.lower():
            return (
                f"{name}'s account is already {current}. "
                "No changes were made."
            )
    except Exception:
        pass
    return None


def _patch_update_url(artifact, params: dict):
    """Replace {member_id} placeholder in the navigate step with the real ID."""
    mid = params.get("member_id", "")
    for step in artifact.steps:
        if step.action == "navigate" and step.input_value and "{member_id}" in step.input_value:
            step.input_value = step.input_value.replace("{member_id}", mid)
            step.input_var   = None
            break


def _format_reply(capability: str, result, params: dict, subject_label: str) -> str:
    if result.outcome_type == "business_outcome":
        return f"⚑ {result.business_outcome}"

    if result.outcome_type == "hard_failure":
        step = result.failed_step_id or "unknown"
        return f"❌ Automation failed at step `{step}`.\n{result.error or ''}"

    if capability == "check_member_balance":
        name    = result.outputs.get("member_name") or subject_label
        balance = result.outputs.get("balance")
        status  = result.outputs.get("account_status", "")
        bal_str = f"${balance:,.2f}" if isinstance(balance, (int, float)) else str(balance)
        return (
            f"✅ **{name}**\n"
            f"• Balance: **{bal_str}**\n"
            f"• Status: **{status}**"
        )

    if capability == "update_account_status":
        name       = result.outputs.get("member_name") or subject_label
        new_status = result.outputs.get("updated_status") or params.get("new_status", "")
        return (
            f"✅ Account updated for **{name}**.\n"
            f"• New status: **{new_status}**"
        )

    return f"✅ Done. {result.outputs}"


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    print("\n  Chat UI → http://localhost:5000")
    print("  Banking app must be running on http://localhost:8080\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
