"""
Mock Banking Back-Office Application
Simulates a legacy bank UI with no clean DOM, no test IDs,
table-based layouts, and unexpected modal dialogs.
This is the "proxy target" the automation system drives.
"""

from flask import Flask, render_template, request, redirect, url_for, jsonify
import random

app = Flask(__name__)

# Simulated member database
MEMBERS = {
    "100001": {"name": "Alice Johnson",    "balance": 4521.00,  "account_status": "Active",   "account_type": "Savings"},
    "100002": {"name": "Bob Martinez",     "balance": 12340.50, "account_status": "Active",   "account_type": "Checking"},
    "100003": {"name": "Carol Williams",   "balance": 750.25,   "account_status": "Frozen",   "account_type": "Savings"},
    "100004": {"name": "David Lee",        "balance": 88000.00, "account_status": "Active",   "account_type": "Investment"},
    "100005": {"name": "Eva Chen",         "balance": 0.00,     "account_status": "Closed",   "account_type": "Checking"},
    "999999": {"name": "Test User",        "balance": 1234.56,  "account_status": "Active",   "account_type": "Savings"},
}


@app.route("/")
def index():
    return redirect(url_for("search"))


@app.route("/search", methods=["GET"])
def search():
    """Search screen - entry point for the automation flow."""
    return render_template("search.html")


@app.route("/search", methods=["POST"])
def search_submit():
    """Handle search form submission."""
    member_id = request.form.get("member_id", "").strip()
    if not member_id:
        return render_template("search.html", error="Please enter a Member ID.")
    return redirect(url_for("member_detail", member_id=member_id))


@app.route("/member/<member_id>")
def member_detail(member_id):
    """Detail screen - shows member info and balance."""
    member = MEMBERS.get(member_id)
    if not member:
        # This is a BUSINESS OUTCOME, not a system error
        return render_template("detail.html", member_id=member_id, not_found=True)
    return render_template("detail.html", member_id=member_id, member=member, not_found=False)


@app.route("/member/<member_id>/update", methods=["GET"])
def update_account(member_id):
    """Update account page with confirmation modal (irreversible action)."""
    member = MEMBERS.get(member_id)
    if not member:
        return redirect(url_for("search"))
    return render_template("update.html", member_id=member_id, member=member)


@app.route("/member/<member_id>/confirm-update", methods=["POST"])
def confirm_update(member_id):
    """Process the confirmed account update."""
    member = MEMBERS.get(member_id)
    if not member:
        return redirect(url_for("search"))
    action = request.form.get("action")
    if action == "confirm":
        # Simulate update
        return render_template("update_success.html", member_id=member_id, member=member)
    return redirect(url_for("member_detail", member_id=member_id))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "mock-bank-ui"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
