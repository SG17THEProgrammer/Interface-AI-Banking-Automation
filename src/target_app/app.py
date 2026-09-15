"""
Mock Banking Back-Office Application
-------------------------------------
Routes only — all state lives in database.py so updates actually persist.
"""

from flask import Flask, render_template, request, redirect, url_for, jsonify
from database import get_member, update_member_status, get_audit_log, list_members, reset_to_seed

import os as _os
_TMPL = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'templates')
app = Flask(__name__, template_folder=_TMPL)


@app.route("/")
def index():
    return redirect(url_for("search"))


# ── Search ────────────────────────────────────────────────────────────────

@app.route("/search", methods=["GET"])
def search():
    return render_template("search.html")


@app.route("/search", methods=["POST"])
def search_submit():
    member_id = request.form.get("member_id", "").strip()
    if not member_id:
        return render_template("search.html", error="Please enter a Member ID.")
    return redirect(url_for("member_detail", member_id=member_id))


# ── Member detail ─────────────────────────────────────────────────────────

@app.route("/member/<member_id>")
def member_detail(member_id):
    member = get_member(member_id)
    if not member:
        return render_template("detail.html", member_id=member_id, not_found=True)
    return render_template("detail.html", member_id=member_id, member=member, not_found=False)


# ── Update account ────────────────────────────────────────────────────────

@app.route("/member/<member_id>/update", methods=["GET"])
def update_account(member_id):
    member = get_member(member_id)
    if not member:
        return redirect(url_for("search"))
    return render_template("update.html", member_id=member_id, member=member)


@app.route("/member/<member_id>/confirm-update", methods=["POST"])
def confirm_update(member_id):
    member = get_member(member_id)
    if not member:
        return redirect(url_for("search"))

    action = request.form.get("action")
    if action == "confirm":
        new_status = request.form.get("new_status", member["account_status"])
        reason_code = request.form.get("reason", "")
        success = update_member_status(
            member_id=member_id,
            new_status=new_status,
            reason_code=reason_code,
            operator=request.form.get("operator", "web-ui"),
        )
        # Re-fetch so the template shows the updated data
        updated_member = get_member(member_id)
        return render_template(
            "update_success.html",
            member_id=member_id,
            member=updated_member,
            new_status=new_status,
            success=success,
        )
    return redirect(url_for("member_detail", member_id=member_id))


# ── Utility / API endpoints ───────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "mock-bank-ui"})


@app.route("/api/members")
def api_members():
    """JSON list of all members — used by the chat UI intent router."""
    return jsonify(list_members())


@app.route("/api/member/<member_id>")
def api_member(member_id):
    member = get_member(member_id)
    if not member:
        return jsonify({"error": "not found"}), 404
    return jsonify({"id": member_id, **member})


@app.route("/api/audit-log")
def api_audit_log():
    return jsonify(get_audit_log())


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Reset all data to seed values (for testing/demos)."""
    reset_to_seed()
    return jsonify({"status": "reset"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)