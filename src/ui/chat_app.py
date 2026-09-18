"""
ui/chat_app.py
--------------
User-facing chat server (port 5000).
"""

from __future__ import annotations
import base64
import json
import logging
import os
import sys
import threading
import time
import uuid
import urllib.request
import hitl_bridge

from flask import Flask, render_template, request, jsonify

_UI_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC    = os.path.dirname(_UI_DIR)
sys.path.insert(0, _SRC)

from capabilities.registry import match, list_capabilities
from engine.engine import ReplayEngine

logger = logging.getLogger(__name__)
app    = Flask(__name__, template_folder=os.path.join(_UI_DIR, "templates"))

EVIDENCE_DIR = os.path.join(_SRC, "..", "evidence")
JOBS_DIR     = os.path.join(EVIDENCE_DIR, "jobs")
BANKING_URL  = "http://localhost:8080"

_jobs: dict[str, dict] = {}
_lock = threading.Lock()
# _hitl_events: dict[str, threading.Event] = {}  # job_id → resume event


# ── Suppress /health and /api/health-proxy from Werkzeug access log ────────

class _HealthFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "/health" not in msg and "/api/health-proxy" not in msg

logging.getLogger("werkzeug").addFilter(_HealthFilter())


# ── Pages ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("chat.html", capabilities=list_capabilities())


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


# ── Chat API ──────────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    data    = request.get_json(force=True)
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "empty message"}), 400

    job_id = str(uuid.uuid4())
    cap    = match(message)

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

    subject_label = _subject_label(cap.parameters)

    if cap.name == "update_account_status":
        warning = _check_already_same_status(cap.parameters)
        if warning:
            return jsonify({
                "job_id":  job_id,
                "type":    "warning",
                "reply":   f"No update needed for {subject_label}.",
                "warning": warning,
            })

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
    # Inject live HITL context if waiting
    if hitl_bridge.is_waiting(job_id):
        ctx = hitl_bridge.get_context(job_id)
        job["status"]       = "hitl_waiting"
        job["hitl_step"]    = ctx.get("step_id")
        job["hitl_reason"]  = ctx.get("reason")
        job["hitl_url"]     = ctx.get("url")
        job["job_id"] = job_id
    return jsonify(job)


@app.route("/api/job/<job_id>/details")
def job_details(job_id: str):
    """
    Return everything about a completed job:
    - the log entries
    - screenshots as base64
    - intervention JSONs
    Used by the dashboard when opened with ?job={job_id}.
    """
    with _lock:
        job = dict(_jobs.get(job_id, {}))
    if not job:
        return jsonify({"error": "unknown job"}), 404

    job_dir = os.path.join(JOBS_DIR, job_id)

    # ── Log entries ───────────────────────────────────────────────────────
    log_entries = job.get("log_entries", [])
    if not log_entries:
        # Try reading from disk if not in memory
        log_path = os.path.join(job_dir, "replay.log")
        if os.path.exists(log_path):
            log_entries = []
            with open(log_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            log_entries.append(json.loads(line))
                        except Exception:
                            pass

    # ── Screenshots as base64 ─────────────────────────────────────────────
    screenshots = []
    ss_dir = os.path.join(job_dir, "screenshots")
    if os.path.isdir(ss_dir):
        for fname in sorted(os.listdir(ss_dir)):
            if fname.lower().endswith((".png", ".jpg", ".jpeg")):
                fpath = os.path.join(ss_dir, fname)
                try:
                    with open(fpath, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    screenshots.append({
                        "name": fname,
                        "size": os.path.getsize(fpath),
                        "data": f"data:image/png;base64,{b64}",
                    })
                except Exception:
                    pass

    # ── Intervention JSONs ────────────────────────────────────────────────
    interventions = []
    iv_dir = os.path.join(job_dir, "interventions")
    if os.path.isdir(iv_dir):
        for fname in sorted(os.listdir(iv_dir)):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(iv_dir, fname)) as f:
                        interventions.append(json.load(f))
                except Exception:
                    pass

    return jsonify({
        "job_id":       job_id,
        "status":       job.get("status"),
        "outcome":      job.get("outcome"),
        "capability":   job.get("capability"),
        "duration":     job.get("duration"),
        "steps":        job.get("steps"),
        "outputs":      job.get("outputs", {}),
        "reply":        job.get("reply", ""),
        "log_entries":  log_entries,
        "screenshots":  screenshots,
        "interventions": interventions,
    })


# ── Proxy endpoints ────────────────────────────────────────────────────────

@app.route("/api/audit-log")
def audit_log():
    try:
        with urllib.request.urlopen(f"{BANKING_URL}/api/audit-log", timeout=3) as r:
            return app.response_class(r.read(), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/job/<job_id>/resolve", methods=["POST"])
def job_resolve(job_id: str):
    """Human has resolved the intervention — unblock the engine thread."""
    if not hitl_bridge.is_waiting(job_id):
        # Already resolved — return ok silently (idempotent)
        return jsonify({"ok": True, "already_resolved": True})

@app.route("/api/job/<job_id>/hitl")
def job_hitl_status(job_id: str):
    """Returns current HITL context if the job is waiting for intervention."""
    if not hitl_bridge.is_waiting(job_id):
        return jsonify({"waiting": False})
    ctx = hitl_bridge.get_context(job_id)
    return jsonify({"waiting": True, **ctx})

@app.route("/api/health-proxy")
def health_proxy():
    try:
        with urllib.request.urlopen(f"{BANKING_URL}/health", timeout=2) as r:
            return app.response_class(r.read(), mimetype="application/json")
    except Exception:
        return jsonify({"status": "offline"}), 502


# ── Background worker ─────────────────────────────────────────────────────

def _run(job_id: str, cap, subject_label: str):
    # Per-job evidence directory
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        hitl_bridge.register_job(job_id)

        # Live progress: write current step to job dict during execution
        # The engine calls this via a callback
        def _on_step(step_id: str, action: str, description: str):
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["live_step"] = {
                        "step_id":     step_id,
                        "action":      action,
                        "description": description,
                    }

        engine = ReplayEngine(
            evidence_dir=EVIDENCE_DIR,
            headless=True,
            max_retries_per_step=2,
            hitl_enabled=True,
        )

        artifact = cap.artifact
        params   = cap.parameters

        if artifact.name == "update_account_status":
            _patch_update_url(artifact, params)

        result = engine.run(
            artifact=artifact,
            parameters=params,
            log_suffix="run",
            non_interactive=False,
            job_dir=job_dir,
            hitl_job_id=job_id,        # engine uses this to signal UI
            on_step_start=_on_step,
        )

        with _lock:
            _jobs[job_id] = {
                "status":        "done",
                "outcome":       result.outcome_type,
                "success":       result.success,
                "outputs":       result.outputs,
                "business_outcome": result.business_outcome,
                "error":         result.error,
                "duration":      round(result.duration_seconds, 2),
                "steps":         result.steps_completed,
                "reply":         _format_reply(cap.name, result, params, subject_label),
                "log_entries":   result.log_entries,
                "job_dir":       job_dir,
            }
            # Clean up the resume event
            hitl_bridge.cleanup(job_id)

    except Exception as e:
        logger.exception(f"[chat] Job {job_id} failed")
        with _lock:
            _jobs[job_id] = {
                "status":  "done",
                "outcome": "hard_failure",
                "success": False,
                "error":   str(e),
                "reply":   f"❌ Automation error: {e}",
                "log_entries": [],
                "job_dir": job_dir,
            }


# ── Helpers ───────────────────────────────────────────────────────────────

def _subject_label(params: dict) -> str:
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
            return f"{name}'s account is already {current}. No changes were made."
    except Exception:
        pass
    return None


def _patch_update_url(artifact, params: dict):
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
        return f"✅ **{name}**\n• Balance: **{bal_str}**\n• Status: **{status}**"
    if capability == "update_account_status":
        name       = result.outputs.get("member_name") or subject_label
        new_status = result.outputs.get("updated_status") or params.get("new_status", "")
        return f"✅ Account updated for **{name}**.\n• New status: **{new_status}**"
    return f"✅ Done. {result.outputs}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    os.makedirs(JOBS_DIR, exist_ok=True)
    print("\n  Chat UI → http://localhost:5000")
    print("  Banking app must be running on http://localhost:8080\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)