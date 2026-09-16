"""
Chat UI — Flask application (port 5000)
----------------------------------------
Routes
  GET  /                      → chat.html
  POST /api/chat              → run engine, return result + job_id
  GET  /api/job/<id>/details  → full evidence bundle for dashboard
  GET  /dashboard             → dashboard.html (auto-loads job if ?job=<id>)
  GET  /api/health-proxy      → proxy to :8080/health (UI polls this)

Health-check requests (/health, /api/health-proxy) are suppressed from
run.log via a Werkzeug log filter so the file stays small.

Per-job evidence layout
  evidence/jobs/{job_id}/
      replay.log
      screenshots/
      interventions/
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
import traceback
import uuid
import urllib.request
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

# ── path setup — must happen before any local imports ─────────────────────
# __file__ = src/ui/chat_app.py  →  dirname×2 = src/
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from artifact import CapabilityArtifact
from capabilities.check_balance import build as build_check_balance_artifact
from capabilities import match as match_capability
from engine.engine import JobReplayEngine

# ── config ────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVIDENCE_DIR = os.path.join(BASE_DIR, "evidence")
ARTIFACT_PATH = os.path.join(EVIDENCE_DIR, "capability_artifact.json")
TARGET_URL   = "http://127.0.0.1:8080"

os.makedirs(os.path.join(EVIDENCE_DIR, "jobs"), exist_ok=True)

# ── Flask app ─────────────────────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
)

# ── Suppress health-check lines from Werkzeug access log ─────────────────
class _HealthFilter(logging.Filter):
    """Drop Werkzeug access-log records for polling endpoints."""
    _SKIP = {"/health", "/api/health-proxy"}

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(skip in msg for skip in self._SKIP)

logging.getLogger("werkzeug").addFilter(_HealthFilter())

# ── Logging ───────────────────────────────────────────────────────────────
os.makedirs(EVIDENCE_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(EVIDENCE_DIR, "run.log"), mode="a"),
    ],
)
logger = logging.getLogger("chat_app")


# ── Helpers ───────────────────────────────────────────────────────────────

def _load_artifact() -> CapabilityArtifact:
    if os.path.exists(ARTIFACT_PATH):
        return CapabilityArtifact.load(ARTIFACT_PATH)
    art = build_check_balance_artifact(target_url=TARGET_URL)
    art.save(ARTIFACT_PATH)
    return art


def _parse_member_id(message: str) -> str | None:
    """Very simple intent parser — extract a 6-digit member ID from the message."""
    import re
    m = re.search(r"\b(\d{6})\b", message)
    return m.group(1) if m else None


def _img_to_b64(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return None


# ── Routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("chat.html")


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """
    Body: { "message": "Check member 100001" }
    Returns: { "reply": "...", "job_id": "...", "result": {...} }
    """
    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "empty message"}), 400

    job_id = f"job_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    logger.info(f"[chat] New job {job_id}: {message!r}")

    # Match intent via registry — supports names ("alice", "carol") and IDs ("100001")
    cap = match_capability(message)
    if not cap:
        return jsonify({
            "reply": (
                "I can look up member balances and update account statuses.\n"
                "Try: \"What is Alice's balance?\" or \"Freeze Carol's account\"."
            ),
            "job_id": None,
            "result": None,
        })

    if cap.missing:
        missing = ", ".join(cap.missing)
        return jsonify({
            "reply": f"I need a bit more info — missing: **{missing}**. Could you clarify?",
            "job_id": None,
            "result": None,
        })

    member_id = cap.parameters.get("member_id", "unknown")

    # Run engine
    try:
        engine = JobReplayEngine(
            base_evidence_dir=EVIDENCE_DIR,
            headless=True,
            hitl_enabled=False,
        )
        result = engine.run(
            artifact=cap.artifact,
            parameters=cap.parameters,
            job_id=job_id,
            non_interactive=True,
        )
    except Exception as e:
        logger.error(f"[chat] Engine error for {job_id}: {e}\n{traceback.format_exc()}")
        return jsonify({
            "reply": f"An internal error occurred: {e}",
            "job_id": job_id,
            "result": None,
        }), 500

    # Build human-readable reply
    if result.success:
        bal = result.outputs.get("balance")
        status = result.outputs.get("status") or result.outputs.get("account_status") or result.outputs.get("updated_status", "")
        name = result.outputs.get("member_name", f"Member {member_id}")
        bal_str = f"${bal:,.2f}" if isinstance(bal, (int, float)) else str(bal) if bal is not None else "—"
        if cap.name == "update_account_status":
            reply = (
                f"✅ **{name}** account updated.\n"
                f"- New Status: **{status}**\n"
                f"- Completed in {result.duration_seconds:.1f}s ({result.steps_completed} steps)"
            )
        else:
            reply = (
                f"✅ **{name}** (ID: {member_id})\n"
                f"- Balance: **{bal_str}**\n"
                f"- Status: **{status}**\n"
                f"- Completed in {result.duration_seconds:.1f}s ({result.steps_completed} steps)"
            )
    elif result.outcome_type == "business_outcome":
        reply = (
            f"⚑ Member **{member_id}** not found in the system.\n"
            f"{result.business_outcome or ''}\n"
            f"Completed in {result.duration_seconds:.1f}s"
        )
    else:
        reply = (
            f"❌ Could not retrieve data for member **{member_id}**.\n"
            f"Error: {result.error or 'unknown'}\n"
            f"Failed at step: {result.failed_step_id or 'unknown'}"
        )

    return jsonify({
        "reply": reply,
        "job_id": job_id,
        "result": {
            "outcome_type": result.outcome_type,
            "success": result.success,
            "outputs": result.outputs,
            "steps_completed": result.steps_completed,
            "duration_seconds": result.duration_seconds,
            "retries_used": result.retries_used,
        },
    })


@app.route("/api/job/<job_id>/details")
def api_job_details(job_id: str):
    """
    Returns the full evidence bundle for one job as JSON:
    {
      job_id, log_entries, screenshots: [{name, b64, size}], interventions: [...]
    }
    """
    # Sanitise job_id — must match expected pattern
    import re
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", job_id):
        return jsonify({"error": "invalid job_id"}), 400

    job_dir = os.path.join(EVIDENCE_DIR, "jobs", job_id)
    if not os.path.isdir(job_dir):
        return jsonify({"error": "job not found"}), 404

    # ── Log entries ──────────────────────────────────────────────────
    log_entries: list[dict] = []
    total_lines = 0
    MAX_LINES = 500
    for fname in sorted(os.listdir(job_dir)):
        if not fname.endswith(".log"):
            continue
        fpath = os.path.join(job_dir, fname)
        with open(fpath, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total_lines += len(lines)
        # Tail-guard: show last MAX_LINES if too large
        for line in lines[-MAX_LINES:]:
            try:
                log_entries.append(json.loads(line))
            except Exception:
                pass

    # ── Screenshots ──────────────────────────────────────────────────
    screenshots = []
    ss_dir = os.path.join(job_dir, "screenshots")
    if os.path.isdir(ss_dir):
        for fname in sorted(os.listdir(ss_dir)):
            if not fname.lower().endswith((".png", ".jpg", ".jpeg")):
                continue
            fpath = os.path.join(ss_dir, fname)
            size = os.path.getsize(fpath)
            b64 = _img_to_b64(fpath)
            if b64:
                screenshots.append({"name": fname, "b64": b64, "size": size})

    # ── Interventions ────────────────────────────────────────────────
    interventions = []
    int_dir = os.path.join(job_dir, "interventions")
    if os.path.isdir(int_dir):
        for fname in sorted(os.listdir(int_dir)):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(int_dir, fname)
            try:
                with open(fpath, encoding="utf-8") as f:
                    interventions.append(json.load(f))
            except Exception:
                pass

    return jsonify({
        "job_id": job_id,
        "total_log_lines": total_lines,
        "truncated": total_lines > MAX_LINES,
        "log_entries": log_entries,
        "screenshots": screenshots,
        "interventions": interventions,
    })


@app.route("/api/health-proxy")
def health_proxy():
    """Proxy the target app's health endpoint — always 200 so browser never logs fetch errors."""
    try:
        with urllib.request.urlopen(f"{TARGET_URL}/health", timeout=3) as resp:
            data = json.loads(resp.read())
            return jsonify({"online": True, **data})
    except Exception as e:
        logger.debug(f"[health-proxy] banking app unreachable: {e}")
        return jsonify({"online": False, "status": "unreachable"})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "chat-ui"})


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("Starting Chat UI on http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)