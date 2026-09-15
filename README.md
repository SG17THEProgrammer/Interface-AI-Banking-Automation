# MemberLink Automation System
### Computer-Use Automation — Redesigned Architecture

A "Record Once → Replay Deterministically" engine with a **user-facing chat interface**.
Users type natural language; the system drives the legacy banking UI and returns structured results.

---

## Quick Start

```bash
# 1. Set up
python -m venv venv && source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# 2. Start everything (chat UI + banking app)
cd src
python main.py                   # defaults to --mode chat

# 3. Open http://localhost:5000 in your browser
```

That's it. The chat UI is at **http://localhost:5000**.
The legacy banking app is at **http://localhost:8080** (for direct inspection).

---

## What You Can Do in the Chat UI

Type natural language — no IDs required if you use a name:

| What you type | What happens |
|---|---|
| `What's Alice's balance?` | Looks up member 100001, returns balance + status |
| `Check member 100002` | Looks up Bob Martinez |
| `What is the status of Carol?` | Returns account status for 100003 |
| `Freeze Carol's account` | Updates 100003 status → Frozen (with modal confirmation) |
| `Close account 100005` | Updates Eva Chen → Closed |
| `Reactivate account 100003` | Sets status → Active |
| `Freeze 100001 reason RISK-001` | Update with a reason code |

The **Audit log** button shows every status change made during the session.

---

## Other Modes

```bash
# Full capability demo — runs balance + update + verify in sequence
python main.py --mode demo

# Replay a specific member without the chat UI
python main.py --mode replay --member 100002

# Start only the banking app (for manual browser testing)
python main.py --mode server

# HITL demo — injects broken locators, shows escalation + intervention file
python main.py --mode hitl-test --auto     # auto-resolve (no browser needed)
python main.py --mode hitl-test            # interactive (browser opens, you fix it)
```

---

## Project Structure

```
interface-ai/
├── requirements.txt
├── README.md
└── src/
    ├── main.py                      ← Thin orchestrator, all modes
    ├── artifact.py                  ← CapabilityArtifact schema (data contract)
    ├── guardrails.py                ← URL allowlist, action gating, PII redaction
    │
    ├── capabilities/                ← Pre-built artifacts for each flow
    │   ├── check_balance.py         ← Balance + status lookup
    │   ├── update_status.py         ← Account status update (with modal)
    │   └── registry.py              ← Intent router: text → capability + params
    │
    ├── engine/                      ← Deterministic replay (no LLM)
    │   ├── engine.py                ← Orchestration loop, ReplayResult
    │   ├── executor.py              ← Single-step execution + checkpoints
    │   ├── extractor.py             ← Output extraction, business-outcome detection
    │   └── locator.py               ← Multi-strategy element resolution
    │
    ├── hitl/                        ← Human-in-the-loop escalation
    │   ├── controller.py            ← Pause/resume lifecycle, intervention JSON
    │   └── annotator.py             ← Screenshot annotation with error banner
    │
    ├── ui/                          ← User-facing interface
    │   ├── chat_app.py              ← Flask chat server (port 5000)
    │   └── templates/chat.html      ← Chat UI — what the user sees
    │
    └── target_app/                  ← Mock legacy banking app (port 8080)
        ├── app.py                   ← Flask routes (thin layer)
        ├── database.py              ← In-memory store with real mutation
        └── templates/               ← Legacy-style HTML (no test IDs)
```

---

## Architecture

```
User types: "Freeze Carol's account"
                ↓
① Intent Router (capabilities/registry.py)
   Scores keywords → update_account_status
   Extracts params → { member_id: "100003", new_status: "Frozen" }
                ↓
② Capability Artifact (capabilities/update_status.py)
   Typed JSON contract: steps, locators, outputs, safety policy
                ↓
③ Replay Engine (engine/engine.py) — no LLM
   Navigate → Select status → Type reason → Click Update → Confirm modal
   Error taxonomy: success | business_outcome | recoverable | hard_failure
                ↓
④ Result → Chat UI
   "✅ Account updated. Carol Williams → Frozen"
   (Audit log updated; status persists in database.py)
```

### What changed from the original design

| Problem | Fix |
|---|---|
| No user interface — only logs | Added `ui/` with a full chat UI at port 5000 |
| HITL required Enter to start | Chat mode runs fully headless; HITL only for `--mode hitl-test` |
| Update flow broken — no persistence | `database.py` module-level dict persists mutations within the process |
| Update modal didn't pass new_status | Fixed `update.html` — JS syncs hidden fields before POST |
| One monolithic file per layer | Split into `engine/`, `hitl/`, `capabilities/` packages |
| Only one capability | Added `update_status` capability with select + modal confirmation |

---

## Test Scenarios

| Member | Name | Balance | Status |
|---|---|---|---|
| `100001` | Alice Johnson | $4,521.00 | Active |
| `100002` | Bob Martinez | $12,340.50 | Active |
| `100003` | Carol Williams | $750.25 | Frozen |
| `100004` | David Lee | $88,000.00 | Active |
| `100005` | Eva Chen | $0.00 | Closed |
| `000000` | — | — | → Business outcome (not found) |

---

## Evidence Files

After running, `evidence/` contains:

| File | What it is |
|---|---|
| `replay_*.log` | NDJSON execution trace per run |
| `screenshots/` | Browser state at key moments |
| `interventions/` | HITL escalation records with annotated screenshots |
