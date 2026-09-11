# REPORT.md — Computer-Use Automation System
## interface.ai Assignment — Design Document

---

## 1. Architecture

### System Overview

This system implements a "Record Once → Replay Deterministically" engine for automating legacy banking UIs that lack modern APIs. It has four layers that work in strict sequence:

```
[Goal + Target URL]
       ↓
① Discovery Agent (LLM in the loop, runs ONCE)
       ↓ produces
② Capability Artifact (typed JSON contract, versioned)
       ↓ consumed by
③ Deterministic Replay Engine (no LLM, runs every time)
       ↓ handles
④ HITL Escalation (human takes live session, then hands back)
```

**Core Abstractions:**

- **`CapabilityArtifact`** — The central data contract. A versioned, typed JSON document declaring what inputs a capability accepts, what steps to perform, what locator strategies to try per step, and what typed outputs to return. Analogous to an OpenAPI spec for UI automation.
- **`DiscoveryAgent`** — Runs an Observe→Decide→Act loop powered by Claude (`claude-sonnet-4-6`) using tool-use (structured function calling). Writes down everything it does into the artifact incrementally.
- **`ReplayEngine`** — Reads the artifact, executes steps via Playwright without any LLM, handles the three-part error taxonomy, and returns a typed `ReplayResult`.
- **`HITLController`** — Pauses the Playwright session (keeping the browser open), signals the operator, and resumes from the same live session after manual intervention.
- **`Guardrails`** — URL allowlist, action safety classification, and PII redaction — consulted by both Discovery and Replay.

**Trade-offs made:**

| Decision | Why |
|---|---|
| Python over TypeScript | Cleaner Anthropic SDK integration, simpler async for this use case |
| Playwright over Selenium | Accessibility tree access, CDP session control for HITL, better locator APIs |
| Tool-use (function calling) over free text | LLM outputs are structured and machine-parseable; no prompt-parsing brittleness |
| CLI-based HITL over web console | Keeps the scope honest; the mechanism is correct and production-upgradable |
| Multi-locator chains in artifact | Primary locator failure doesn't crash replay; the chain tries fallbacks automatically |

---

## 2. Artifact Schema

### Design Rationale

The artifact is **not** a chat log or a recording of raw Playwright commands. It is an **engine-executable contract** — structured so the Replay engine can operate it without knowing anything about how it was discovered.

**Key schema decisions:**

**Locator chains** — Every action step carries an ordered `locators` array, not a single selector:
```json
"locators": [
  {"strategy": "aria-label",  "value": "Member ID",              "description": "Most stable"},
  {"strategy": "placeholder", "value": "Enter 6-digit Member ID","description": "Fallback 1"},
  {"strategy": "css",         "value": "input[name='member_id']","description": "Fallback 2"},
  {"strategy": "css",         "value": "input[type='text']:first-of-type", "description": "Last resort"}
]
```
The locator strategies are ordered from most stable (`aria-label`, semantic attributes) to least stable (`css`, `xpath`). If the vendor updates their markup, the first strategy that still works wins. This is the key mechanism for handling UI drift without rebuilding from scratch.

**Typed outputs** — Outputs declare their type and extractor, so the Replay engine performs coercion (`"$4,521.00"` → `4521.00`) and the caller gets a typed value:
```json
{"name": "balance", "type": "float", "transform": "text"}
```

**Parameterized inputs** — Steps that accept variable values at runtime use `input_var` instead of `input_value`, creating a clean separation between the flow definition and the runtime invocation:
```json
{"action": "type", "input_var": "member_id"}   // definition
```
```python
engine.run(artifact, parameters={"member_id": "100002"})  // invocation
```

**Safety policy embedded** — The artifact carries its own safety metadata, so any system loading the artifact knows what it does before running it:
```json
"safety": {
  "allowed_domain": "localhost:8080",
  "has_irreversible_actions": false,
  "pii_fields": ["member_name"]
}
```

---

## 3. Determinism & Error Handling

### The Three-Part Error Taxonomy

The most important design decision in the Replay engine. Correctly classifying outcomes prevents both false alarms and silent failures.

**1. Business Outcome** — A legitimate result that is not a system error. The member search page showing "No record found for Member ID: 000000" is the system working correctly. The Replay engine detects this via pattern matching on page text and the `#member-not-found` element, and returns:
```json
{"outcome_type": "business_outcome", "success": false, "business_outcome": "No record found..."}
```
The caller handles this the same way they'd handle a 404 from a REST API — as meaningful data, not an exception.

**2. Recoverable Condition** — Transient states the engine handles itself without escalation. Cookie banners, brief loading spinners, modal dialogs with a clear dismiss button. The engine calls `try_recover_page()` before each step, clicks the dismiss, and continues.

**3. Hard Failure** — Genuine execution errors: the Search button literally doesn't exist, a network timeout, a selector that resolves to zero elements after all fallbacks are exhausted. Returns a full debug context:
```json
{
  "outcome_type": "hard_failure",
  "failed_step_id": "s3_click_search",
  "expected_state": "Click the Search button",
  "observed_state": "Error: All 3 locator strategies exhausted",
  "screenshot_path": "evidence/screenshots/failure_s3_click_search.png"
}
```

### Locator Fallback Logic

Every step iterates its `locators` array in order. The first locator that resolves to a visible element wins. All failures are logged at DEBUG level, so the evidence shows exactly which strategy ultimately worked.

### Checkpoint Assertions

After every significant step, the engine asserts it actually reached the expected state. Example: after clicking Search, the checkpoint asserts `url_contains="/member/"`. This catches silent navigation failures that Playwright wouldn't throw on by itself.

### Retry Policy

Each step gets `max_retries_per_step` (default: 2) attempts. Between retries, the engine calls `try_recover_page()` in case a transient condition appeared. After all retries fail, the engine escalates to HITL before declaring hard failure.

---

## 4. Heterogeneity & Multi-Tenant

### Surface Heterogeneity

This system is designed for a web browser target. To extend it to native Windows desktop banking cores (which many credit unions run), the architecture requires:

- **Replace the page observer**: Instead of Playwright's accessibility tree, use `pywinauto` (Windows UI Automation API) or `AT-SPI` (Linux accessibility). The observer contract remains the same — it returns a snapshot dict — only the implementation changes.
- **Replace the action executor**: Map step actions (`click`, `type`) to OS-level API calls instead of Playwright calls. The Step schema doesn't change.
- **Replace the locator strategies**: Add `win32_control_id`, `automation_id`, and `name+class` strategies alongside the existing web strategies.

The artifact schema is surface-agnostic by design. A `Step` with `action="click"` and `locators=[{strategy: "automation_id", value: "btnSearch"}]` would work identically in a Windows executor.

For **frameset legacy apps** (20-year-old bank UIs with nested frames), Playwright handles these via `frame_locator()`. The locator strategy would need a `frame_selector` field added to target elements inside a specific frame. This is a schema extension, not a redesign.

### Multi-Tenant Scale

500 banks may run Jack Henry (or another vendor's software) with identical flows but different:
- Color themes (irrelevant to automation)
- Custom field labels ("Member ID" vs "Account Number")
- URL structures (`/member/` vs `/accounts/`)
- Injected custom fields

**Solution: Tenant-specific locator overrides.**

The artifact's `locators` array is the override point. The base artifact ships with the common locator chain. A tenant-specific override file injects additional or replacement locators at the front of the chain:

```json
// base artifact step s2
"locators": [{"strategy": "aria-label", "value": "Member ID"}, ...]

// tenant_jackhenry_override.json
{"step_id": "s2_type_member_id", "locators": [
  {"strategy": "aria-label", "value": "Account Number"},  // ← tenant specific
  {"strategy": "css", "value": "input[name='acct_no']"}   // ← tenant specific
  // base fallbacks still apply after these
]}
```

The Replay engine merges tenant overrides before execution. This means one discovery run can serve 500 tenants, with only their locator differences captured in small override files — not full re-recordings.

---

## 5. Escalation & Handoff

### When Escalation Triggers

The HITL controller escalates in two cases:
1. **Discovery agent calls `escalate_to_human`** — when the LLM has been stuck on the same action for 2+ consecutive attempts.
2. **Replay engine exhausts all retries on a step** — when it cannot proceed and HITL is enabled.

### Session Preservation (Critical)

The Playwright `Browser` and `Page` objects are kept alive during escalation. The human operator interacts with the **exact same browser instance** — not a new window, not a screenshot. This is essential because:
- The session may carry authentication cookies
- The app state (filled forms, navigated pages) must be preserved
- Closing and reopening would lose all context

### Control Transfer Protocol

```
Automation detects stuck state
  → Takes screenshot of current state
  → Writes InterventionRequest to evidence/interventions/
  → Prints structured notice to terminal:
       [INTERVENTION REQUIRED] Blocked at step s3. URL: ...
       Browser is open. Please resolve, then press ENTER.
  → Blocks on input()
Human resolves (clicks away popup, solves 2FA, confirms risky action)
  → Presses ENTER (with optional notes)
Automation reads new page.url
  → Logs resolution with duration + human notes
  → Continues next step
```

### Production Upgrade Path

In production, `input()` is replaced with a REST endpoint that the operator UI calls. The `InterventionRequest` is already serialized to JSON and could be routed to Slack, a ticket system, or a web console. The mechanism is identical; only the transport changes.

---

## 6. Safety

### Domain Allowlist

The `Guardrails` class checks every URL before navigation. The allowlist is configured per-capability in `GuardrailPolicy.allowed_domains` and `allowed_path_prefixes`. Navigation to any URL outside the allowlist raises a `GuardrailViolation` and halts execution — it does not silently proceed or log and continue.

### Action Safety Classification

Every `Step` carries an `is_reversible` boolean. Steps marked `is_reversible=False` are:
- Logged with a WARNING before execution
- Flagged in the artifact's `safety.irreversible_step_ids` list
- Subject to the `block_irreversible_actions` policy (which can halt execution entirely)

In the demo capability (`check_member_balance`), all steps are reversible (read-only). The update flow (`update.html`) demonstrates an irreversible action — clicking "Yes, Update" in the confirmation modal — which would be flagged before execution in a production deployment.

### PII Redaction

The `Guardrails.redact()` method applies regex patterns for SSNs, credit card numbers, auth tokens, and password fields before any value is written to a log file or the artifact. The `pii_fields` list in the artifact's safety policy identifies output fields that must be redacted in logs (though their actual extracted values are returned to the caller through the secure result object, not logged).

Bank records contain SSNs, full account numbers, and balances. None of these are ever written raw to `discovery_run.log` or `replay_success.log`.

---

## 7. Cuts

### What Was Deliberately Not Built

**1. REST API / Web console for HITL**
Built as CLI (`input()`). The intervention request is fully serialized to JSON and the resume mechanism is one function call — plugging in a WebSocket or REST handler is a one-day engineering task. The CLI version is complete and correct for demonstrating the mechanism.

**2. UI drift detection**
The system handles drift reactively (locator fallbacks kick in when the primary breaks) but not proactively (no scheduled re-validation runs). In production, drift detection would replay the artifact on a schedule, compare outputs to a baseline, and flag checkpoints that start failing before they affect real users.

**3. Tenant override file loader**
The schema and architecture for tenant overrides is designed (see Section 4). The merge logic was scoped out of this submission to keep the replay engine focused and testable.

**4. Encrypted credential store**
The target app requires no login for this demo. In production, credentials would be injected at runtime from a secrets manager (HashiCorp Vault, AWS Secrets Manager) — never stored in the artifact or logged.

**5. Vision/coordinate-based fallback**
If all accessibility tree and DOM locator strategies fail, the final fallback would be sending a screenshot to a vision model and asking it to identify the element's coordinates. Not implemented here — the accessibility tree is sufficient for the demo target, and vision is expensive enough that it should be a last resort with its own cost guardrails.

### What Would Be Built Next

1. Artifact versioning + drift alerting: compare live locator success rates against baseline, auto-create a rediscovery task when a locator chain starts failing
2. Tenant override management UI: operators upload small JSON override files per tenant without re-running full discovery
3. Capability registry: a catalogue of all saved artifacts with their parameters, outputs, and safety classifications — queryable by a planning agent that needs to choose which capability to invoke
4. Cost tracking: each discovery run logs token usage; the registry shows per-capability LLM cost amortized over N replay calls
