# REPORT.md — Computer-Use Automation System
## interface.ai Assignment — Design Document

---

## 1. Architecture

### System Overview

This system implements a "Record Once → Replay Deterministically" engine for automating legacy banking UIs that lack modern APIs. It has four layers that work in strict sequence:


````

[Goal + Target URL]

        ↓

① Discovery Agent  (LLM in the loop, runs ONCE)

        ↓ produces

② Capability Artifact  (typed JSON contract, versioned)

        ↓ consumed by

③ Deterministic Replay Engine  (no LLM, runs every time)

        ↓ handles

④ HITL Escalation  (human takes live session, then hands back)

````

**Core Abstractions:**

- **`CapabilityArtifact`** — The central data contract. A versioned, typed JSON document declaring what inputs a capability accepts, what steps to perform, what locator strategies to try per step, and what typed outputs to return.
- **`DiscoveryAgent`** — Runs an Observe→Decide→Act loop powered by an LLM (Groq, `openai/gpt-oss-safeguard-20b`) using tool-use (structured function calling). Writes down everything it does into the artifact incrementally.
- **`ReplayEngine`** — Reads the artifact, executes steps via Playwright without any LLM, handles the three-part error taxonomy, and returns a typed `ReplayResult`.
- **`HITLController`** — Pauses the Playwright session (keeping the browser open), signals the operator, and resumes from the same live session after manual intervention. In chat mode, signals the Flask server via a shared `threading.Event` in `hitl_bridge.py` instead of blocking `input()`.
- **`Guardrails`** — URL allowlist, action safety classification, and PII redaction — consulted by both Discovery and Replay.
- **`chat_app.py`** — Flask server (port 5000) providing the chat UI, job polling API, per-job evidence API for the dashboard, and the `/api/job/{id}/resolve` endpoint that the operator clicks to unblock the engine thread.

**Evolution from initial design:** Started with single-file design which was refactored into `engine/`, `hitl/`, and `capabilities/` packages, and a full chat UI was added later for better UX. The `hitl_bridge.py` module was introduced to decouple the Flask server from the HITL controller without a circular import — both import the tiny bridge module, which holds the shared `threading.Event` and HITL context per job.

**Trade-offs made:**

| Decision | Why |
| Python over TypeScript | Cleaner Groq SDK integration, simpler threading for HITL |
| Playwright over Selenium | Accessibility tree access, CDP session control for HITL, better locator APIs |
| Tool-use (function calling) over free text | LLM outputs are structured and machine-parseable; no prompt-parsing brittleness |
| Flask + threading over async | HITL requires blocking a thread; `threading.Event` is the simplest correct mechanism |
| CLI-based HITL signal → REST endpoint | The mechanism is suitable and production-upgradable; `input()` is replaced by a POST to `/api/job/{id}/resolve` in chat mode |
| Per-job evidence directories | Keeps each chat message's evidence isolated; dashboard loads any job by ID without parsing a global log |
| Multi-locator chains in artifact | Primary locator failure doesn't crash replay; the chain tries fallbacks automatically |

---

## 2. Artifact Schema

### Design Rationale

The artifact is **not** a chat log or a recording of raw Playwright commands. It is an **engine-executable contract** — structured so the Replay engine can operate it without knowing anything about how it was discovered.

**Key schema decisions:**

**Locator chains** — Every action step carries an ordered `locators` array, not a single selector:
```json
"locators": [
  {"strategy": "aria-label",  "value": "Member ID",               "description": "Most stable"},
  {"strategy": "placeholder", "value": "Enter 6-digit Member ID", "description": "Fallback 1"},
  {"strategy": "css",         "value": "input[name='member_id']", "description": "Fallback 2"},
  {"strategy": "css",         "value": "input[type='text']:first-of-type", "description": "Last resort"}
]

````

Strategies are ordered from most stable (semantic attributes like `aria-label`) to least stable (`css`, `xpath`). If the vendor updates their markup, the first strategy that still resolves to a visible element wins. This is the key mechanism for handling UI drift without rebuilding from scratch.

**Typed outputs** — Outputs declare their type and extractor so the Replay engine performs coercion (`"$4,521.00"` → `4521.00`) and the caller gets a typed value:

```json
{"name": "balance", "type": "float", "transform": "text"}

```

**Parameterized inputs** — Steps that accept variable values use `input_var` instead of `input_value`, creating a clean separation between the flow definition and runtime invocation:

```json
{"action": "type", "input_var": "member_id"}      // definition
engine.run(artifact, parameters={"member_id": "100002"})  // invocation

```

**Checkpoint assertions** — Each step optionally carries a `checkpoint` that the engine verifies after execution:

```json
{"type": "url_contains", "value": "/member/"}
{"type": "element_visible", "value": "#balance-amount, #member-not-found"}

```

This catches silent navigation failures that Playwright wouldn't throw on by itself.

**Safety policy embedded** — The artifact carries its own safety metadata so any system loading the artifact knows what it does before running it:

```json
"safety": {
  "allowed_domain": "localhost:8080",
  "has_irreversible_actions": true,
  "irreversible_step_ids": ["s4_click_update", "s5_confirm_modal"],
  "pii_fields": ["member_name"]
}

```

**Surface agnosticism** — The schema deliberately contains no web-specific concepts. A `Step` with `action="click"` and `locators=[{strategy: "automation_id", value: "btnSearch"}]` would be valid for a Windows desktop executor. Only the executor layer is surface-specific.

---

## 3. Determinism & Error Handling

### The Three-Part Error Taxonomy

The most important design decision in the Replay engine. Correctly classifying outcomes prevents both false alarms and silent failures.

**1. Business Outcome** — A legitimate result that is not a system error. The member search page showing "No record found for Member ID: 000000" is the system working correctly. The Replay engine detects this via `BUSINESS_OUTCOME_PATTERNS` regex matching on page body text and the `#member-not-found` element, and returns:

```json
{"outcome_type": "business_outcome", "success": false, "business_outcome": "No record found..."}

```

The caller handles this the same way they'd handle a 404 from a REST API — as meaningful data, not an exception. The chat UI renders it as a `⚑` symbol, not an error.

**2. Recoverable Condition** — Transient states the engine handles itself without escalation. Cookie banners, brief loading spinners, modal dialogs with a clear dismiss button. The engine calls `try_recover_page()` before each step, which clicks known dismissable selectors and continues.

**3. Hard Failure** — Genuine execution errors: the Search button literally doesn't exist after all fallbacks are exhausted, a network timeout, a guardrail violation. Returns a full debug context:

```json
{
  "outcome_type": "hard_failure",
  "failed_step_id": "s3_click_search",
  "expected_state": "Click the Search button",
  "observed_state": "Error: All 2 locator strategies exhausted",
  "screenshot_path": "evidence/screenshots/failure_s3_click_search.png"
}

```

### Locator Fallback Logic

`engine/locator.py`'s `resolve_element()` iterates the step's `locators` array in order. The first locator that resolves to a **visible** element wins (`.wait_for(state="visible")`). All failures are logged at DEBUG, so the evidence shows exactly which strategy ultimately worked (`strategy_used` field in every `step_done` log entry).

### Checkpoint Assertions

After every significant step, the engine asserts it actually reached the expected state. After clicking Search, the checkpoint asserts `url_contains="/member/"`. After clicking Update Account, it asserts `element_visible="#confirmModal"`. This catches silent navigation failures and stale-page situations that Playwright wouldn't throw on.

### Retry Policy

Each step gets `max_retries_per_step` attempts (default: 2). Between retries the engine calls `try_recover_page()` in case a transient condition appeared. After all retries fail, the engine escalates to HITL before declaring hard failure.

### UI Drift

The system handles drift **reactively**: when the primary locator breaks, the fallback chain tries the next strategy. It does not handle drift **proactively** — scheduled re-validation is in the cut list (Section 7). The locator chain ordering (semantic → structural → positional) means the most drift-resistant strategies are tried first, so minor markup changes don't surface as failures.

---

## 4. Heterogeneity & Multi-Tenant

### Surface Heterogeneity

The system targets web browsers. The artifact schema is surface-agnostic by design — it contains no web-specific concepts. To extend it:

**Native Windows desktop apps** (which many credit unions run on Jack Henry or similar cores):

- Replace `engine/locator.py`'s `_build_locator()` with `pywinauto` (Windows UI Automation API) or `AT-SPI` (Linux). The locator contract — "try this list of strategies in order, return the first visible element" — is identical.
- Add `win32_control_id` and `automation_id` as valid strategy values. The Step schema doesn't change.
- Replace `engine/executor.py`'s Playwright calls with OS-level API calls (`el.click()` stays the same interface; only the implementation changes).

**Frameset legacy apps** (20-year-old bank UIs with nested frames): Playwright handles these via `frame_locator()`. The locator schema needs a `frame_selector` field to target elements inside a specific frame — a one-field schema extension, not a redesign.

**Screenshot / vision fallback**: If all accessibility-tree and DOM strategies fail, the final fallback would send a screenshot to a vision model and ask it to identify the element's coordinates. Not implemented — the accessibility tree is sufficient for the demo target, and vision is expensive enough to warrant its own cost guardrails.

### Multi-Tenant Scale

500 banks may run Jack Henry with identical flows but different field labels ("Member ID" vs "Account Number"), URL structures, or injected custom fields.

**Solution: Tenant-specific locator overrides.**

The artifact's `locators` array is the override point. The base artifact ships with the common locator chain. A small tenant-specific override file prepends additional or replacement locators:

```json
// base artifact step s2_type_id
"locators": [{"strategy": "aria-label", "value": "Member ID"}, ...]

// tenant_jackhenry_acme_overrides.json
{"step_id": "s2_type_id", "locators": [
  {"strategy": "aria-label", "value": "Account Number"},   // ← tenant-specific
  {"strategy": "css",        "value": "input[name='acct_no']"}  // ← tenant-specific
  // base fallbacks still apply after these
]}

```

The Replay engine merges tenant overrides at load time. One discovery run serves 500 tenants, with only their locator differences captured in small JSON files — not full re-recordings. The schema for this is designed; the file loader and merge logic is in the cut list (Section 7) to keep the core engine focused and testable.

**Drift per tenant**: Each tenant's override file can also carry a `last_verified` timestamp. A scheduled replay against each tenant would compare locator success rates to a baseline and flag the tenant when its primary locator starts failing, triggering a targeted re-discovery of just that step.

---

## 5. Escalation & Handoff

### When Escalation Triggers

The HITL controller escalates in two cases:

1. **Discovery agent calls** **`escalate_to_human`** — when the LLM detects it has been stuck on the same action for 2+ consecutive attempts.
2. **Replay engine exhausts all retries on a step** — when `max_retries_per_step` attempts all fail and `hitl_enabled=True`.

### Session Preservation (Critical)

The Playwright `Browser`, `BrowserContext`, and `Page` objects are kept alive during escalation. In interactive terminal mode, the human operator interacts with the **exact same browser instance** — not a new window, not a screenshot. This is essential because:

- The session carries authentication cookies
- The app state (filled forms, navigated pages) must be preserved
- Closing and reopening would lose all context

In chat UI mode, the bot runs headless (no visible browser), so the operator resolves by navigating to the banking app in their own browser tab and performing the action there. The engine's headless Playwright page navigates to the resolved URL (derived from `parameters["member_id"]`) after the operator clicks Resume.

### Control Transfer Protocol — Two Modes

**Terminal / interactive mode** (used by `--mode hitl-test`):

```
Automation detects stuck state
  → Takes raw screenshot
  → Annotates screenshot with red error banner (step ID, reason, URL, timestamp)
  → Writes InterventionRequest to evidence/interventions/
  → Prints structured notice to terminal
  → Blocks on input()
Human resolves in the live browser, presses ENTER
Automation reads new page.url
  → Logs resolution with duration + human notes
  → Continues next step

```

**Chat UI mode** (used by the web interface):

```
Automation detects stuck state
  → Takes + annotates screenshot, writes InterventionRequest
  → Calls hitl_bridge.signal_waiting(job_id, step_id, reason, url, ...)
  → Engine thread blocks on threading.Event (10-minute timeout)
Chat server's /api/job/{id} poll returns status="hitl_waiting" with context
UI renders HITL banner with step details and "I've fixed it — Resume" button
Operator navigates to banking app, performs action manually, clicks Resume
  → POST /api/job/{id}/resolve
  → hitl_bridge.resolve(job_id, notes) sets the threading.Event
Engine thread unblocks
  → Navigates headless Playwright page to member detail URL
  → Logs hitl_resolved, continues next step

```

### Production Upgrade Path

The `input()` in terminal mode and the `threading.Event` in chat mode are both replaced by the same production primitive: a REST endpoint or WebSocket message. The `InterventionRequest` is already serialized to JSON and could be routed to Slack, a ticketing system, or an operator console. The mechanism is identical; only the transport changes.

---

## 6. Safety

### Domain Allowlist

`Guardrails.check_url()` validates every URL before navigation against `GuardrailPolicy.allowed_domains` and `allowed_path_prefixes`. Navigation to any URL outside the allowlist raises a `GuardrailViolation` and **halts execution** — it does not silently proceed or log and continue. The discovery log shows this working: the agent's first attempt to navigate to the bare base URL (`http://localhost:8080`) was blocked because the path `""` is not in `["/search", "/member/", "/health", "/api/"]`.

### Action Safety Classification

Every `Step` carries an `is_reversible` boolean. Steps marked `is_reversible=False` are:

- Logged with a WARNING before execution
- Listed in the artifact's `safety.irreversible_step_ids`
- Subject to the `block_irreversible_actions` policy (which can halt execution entirely if set)

The `check_member_balance` capability is entirely read-only — all steps are reversible. The `update_account_status` capability flags `s4_click_update` and `s5_confirm_modal` as irreversible — clicking "Update Account" and "Yes, Confirm Update" in the modal cannot be undone.

### PII Redaction

`Guardrails.redact()` applies regex patterns for SSNs, credit card numbers, auth tokens, and password fields before any value is written to a log file. `pii_fields` in the artifact's safety policy identifies output fields whose values are redacted in logs (though returned unredacted to the caller through the secure `ReplayResult` object).

In the demo, `member_name` is declared a PII field. Its value appears in the `output_extracted` log event as `[REDACTED]` but is returned correctly to the chat UI which displays it to the operator.

### Limits

The current guardrail model is policy-as-code, not policy-as-service. A production system would externalise the policy to a control plane that operators can update without a code deploy. The allowlist currently covers a single tenant's domain; multi-tenant deployment would require per-artifact policy objects loaded at runtime.

---

## 7. Cuts

### What Was Deliberately Not Built

**1. Tenant override file loader**

The schema and architecture for tenant overrides is fully designed (see Section 4). The merge logic — loading a small JSON override file and prepending its locators to the artifact's chains before execution — was scoped out to keep the replay engine focused and testable. This is a two-hour engineering task on top of the existing schema.

**2. Proactive drift detection**

The system handles drift reactively (locator fallbacks kick in when the primary breaks) but not proactively (no scheduled re-validation runs). Production deployment would replay each artifact on a schedule, compare locator success rates to a baseline, and auto-create a rediscovery task when a locator chain starts degrading.

**3. Capability registry API**

The capabilities exist as Python modules. A production system would expose them as a callable catalog (`GET /capabilities`, `POST /capabilities/{name}/invoke`) that an AI planning agent could discover and invoke by name with typed args. The schema (name, parameters, outputs, safety) is already present in the artifact — surfacing it as an API is a thin layer on top.

**4. Vision/coordinate-based fallback**

If all accessibility-tree and DOM locator strategies fail, the final fallback would send a screenshot to a vision model and ask it to identify the element's coordinates. The accessibility tree is sufficient for the demo target; vision is expensive enough to warrant its own cost guardrails and is a clean extension point.

**5. Desktop surface executor**

The architecture explicitly supports it (swap `engine/locator.py` and `engine/executor.py` implementations), but only the web executor is implemented.

**6. Multi-run stability signal**

No flakiness tracking. Replaying N times and reporting per-step failure rates would surface which locators are marginal before they affect real users.

### What Would Be Built Next (in priority order)

1. **Capability registry API** — expose artifacts as callable tools that an AI agent can discover and invoke; this is the most direct path to the real product value
2. **Artifact versioning + drift alerting** — compare live locator success rates against baseline, auto-create a rediscovery task when a chain starts failing
3. **Tenant override file loader** — operators upload small JSON override files per tenant without re-running full discovery; the schema is already designed
4. **Cost tracking** — each discovery run logs token usage; the registry shows per-capability LLM cost amortized over N replay calls
5. **Vision fallback** — screenshot → vision model → coordinates, as a last resort after all DOM strategies fail
6. **Multi-run stability signal** — replay N times, report a flakiness percentage per step, gate unattended replay on a stability threshold

```