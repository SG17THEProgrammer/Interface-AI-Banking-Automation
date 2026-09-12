# Computer-Use Automation System
### interface.ai Assignment — Submission

A "Record Once → Replay Deterministically" engine for automating legacy banking UIs that have no modern APIs.

---

## Quick Start (5 minutes)

```bash
# 1. Clone / enter the project
cd interface-ai

# 2. Create a virtual environment
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install Playwright browsers
playwright install chromium

# 5. Set your Anthropic API key
export GROQ_API_KEY=sk-ant-...    # Windows: set GROQ_API_KEY=sk-ant-...

# 6. Run the full system
cd src
python main.py --mode all
```

That's it. The run takes 60–120 seconds and produces all required evidence files.

---

## Project Structure

```
interface-ai/
├── README.md                         ← You are here
├── REPORT.md                         ← 7-section design document
├── requirements.txt
├── evidence/                         ← Generated evidence (gitignored except artifact)
│   ├── capability_artifact.json      ← Pre-built reference artifact (also output by discovery)
│   ├── discovery_run.log             ← LLM-driven run transcript (generated)
│   ├── replay_success.log            ← Successful replay trace (generated)
│   ├── replay_failure.log            ← Business outcome / failure replay (generated)
│   ├── replay_success_summary.json   ← Typed output summary (generated)
│   ├── replay_failure_summary.json   ← Failure summary (generated)
│   ├── screenshots/                  ← Browser screenshots at key moments
│   └── interventions/                ← HITL escalation records (if any)
└── src/
    ├── main.py                       ← Entry point / orchestrator
    ├── discovery.py                  ← LLM discovery agent
    ├── replay.py                     ← Deterministic replay engine
    ├── artifact.py                   ← Capability artifact schema + serialization
    ├── guardrails.py                 ← Safety: allowlist, action gating, PII redaction
    ├── hitl.py                       ← Human-in-the-loop escalation
    └── target_app/
        ├── app.py                    ← Mock banking Flask app
        └── templates/
            ├── search.html           ← Search screen (legacy-style markup)
            ├── detail.html           ← Member detail + balance screen
            ├── update.html           ← Update account + confirmation modal
            └── update_success.html   ← Post-update confirmation
```

---

## Running Each Mode

### Full end-to-end (recommended for first run)
```bash
python main.py --mode all
```
Starts the mock app → runs LLM discovery → runs replay (success + failure cases) → prints evidence summary.

### Discovery only
```bash
python main.py --mode discovery
```
Runs only the LLM agent phase. Saves `capability_artifact.json` and `discovery_run.log`.

### Replay only (no API key needed)
```bash
python main.py --mode replay --use-prebuilt-artifact
```
Skips discovery and uses the pre-built reference artifact. Perfect for testing replay without spending API tokens.

### Replay for a specific member
```bash
python main.py --mode replay --member 100002
python main.py --mode replay --member 000000   # will produce business outcome
```

### With visible browser window
```bash
python main.py --mode all --visible
```
Opens a real Chrome window so you can watch the automation run.

### Target app only (for manual testing)
```bash
python main.py --mode server
# Then open http://localhost:8080 in your browser
```

---

## Test Scenarios

| Member ID | Name | Expected Outcome |
|---|---|---|
| `100001` | Alice Johnson | ✅ Success — balance: $4521.00, Active |
| `100002` | Bob Martinez | ✅ Success — balance: $12340.50, Active |
| `100003` | Carol Williams | ✅ Success — balance: $750.25, **Frozen** |
| `100004` | David Lee | ✅ Success — balance: $88000.00, Active |
| `100005` | Eva Chen | ✅ Success — balance: $0.00, **Closed** |
| `999999` | Test User | ✅ Success — balance: $1234.56, Active |
| `000000` | N/A | ⚑ Business outcome — "No record found" |
| `BADTYPE` | N/A | ❌ Hard failure — parameter validation error (not a valid 6-digit ID) |

---

## What Gets Generated

After running `python main.py --mode all`, the `evidence/` directory contains:

| File | What it is |
|---|---|
| `capability_artifact.json` | The typed JSON contract for the `check_member_balance` capability |
| `discovery_run.log` | Newline-delimited JSON: every LLM call, tool invocation, and page observation |
| `replay_success.log` | Newline-delimited JSON: deterministic execution trace for member 100001 |
| `replay_failure.log` | Newline-delimited JSON: business outcome trace for member 000000 |
| `replay_success_summary.json` | Structured `ReplayResult` with typed outputs |
| `replay_failure_summary.json` | Structured `ReplayResult` showing business outcome handling |
| `screenshots/discovery_final.png` | Browser state at end of discovery run |
| `screenshots/replay_final.png` | Browser state at end of successful replay |

---

## Prerequisites

- **Python 3.10+**
- **pip** (comes with Python)
- **Anthropic API key** — required only for `--mode discovery`. Not needed for `--mode replay`.
- **Port 8080** — must be free for the mock banking app.

### Verify installation
```bash
python --version         # Should be 3.10+
python -c "import anthropic; print(anthropic.__version__)"
python -c "import playwright; print('playwright ok')"
python -c "import flask; print(flask.__version__)"
```

---

## Common Issues & Fixes

**Port 8080 already in use**
```bash
# Find and kill whatever is using it:
lsof -i :8080 | grep LISTEN       # macOS/Linux
netstat -ano | findstr :8080       # Windows
```

**Playwright browsers not installed**
```bash
playwright install chromium
# or if that doesn't work:
python -m playwright install chromium
```

**API key not recognized**
```bash
# Verify it's set:
echo $GROQ_API_KEY       # macOS/Linux
echo %GROQ_API_KEY%      # Windows
```

**Discovery times out or gets stuck**
The discovery agent has a 20-step limit. If the LLM is confused, try running with `--visible` to watch what's happening, or use `--use-prebuilt-artifact` to skip straight to replay.

**ModuleNotFoundError**
Make sure you're running from inside the `src/` directory:
```bash
cd interface-ai/src
python main.py --mode all
```

---

## Architecture in Brief

```
Goal: "Find member 100001, get their balance"
              ↓
① Discovery Agent (LLM loop — runs ONCE)
   Observe page → Ask Claude what to do → Execute in Playwright → Repeat
   Output: capability_artifact.json + discovery_run.log
              ↓
② Capability Artifact (JSON contract)
   Declares: inputs, steps, locator chains, output types, safety policy
              ↓
③ Replay Engine (no LLM — runs every time)
   Reads artifact → Executes steps → Returns typed outputs
   Error taxonomy: success | business_outcome | recoverable | hard_failure
              ↓ (if stuck)
④ HITL Escalation
   Keeps browser open → Signals operator → Waits for ENTER → Resumes
```

See `REPORT.md` for the full 7-section design document.

---

## Demo Commands for Evaluators

```bash
# Setup (one time)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt && playwright install chromium
export GROQ_API_KEY=gsk_.....

# Full run (all phases)
cd src && python main.py --mode all --visible

# Replay only — no API key needed
cd src && python main.py --mode replay --use-prebuilt-artifact

# Test specific members
python main.py --mode replay --use-prebuilt-artifact --member 100003  # frozen account
python main.py --mode replay --use-prebuilt-artifact --member 000000  # business outcome

# Inspect the target app manually
python main.py --mode server
# Open http://localhost:8080 in browser
```
