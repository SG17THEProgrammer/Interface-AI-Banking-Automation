"""
Discovery Agent — LLM-Driven Automation Loop (Groq backend)
-------------------------------------------------------------
Uses Groq's OpenAI-compatible API with tool-use (function calling).
Model: openai/gpt-oss-20b (supports parallel tool calls, fast, free tier)

Get a free API key at: https://console.groq.com
Set it with: set GROQ_API_KEY=gsk_...
"""

from __future__ import annotations
import json
import os
import time
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional

from openai import OpenAI
from playwright.sync_api import sync_playwright, Page

from artifact import (
    CapabilityArtifact, Step, Locator, Checkpoint,
    OutputField, SafetyPolicy
)
from guardrails import Guardrails, GuardrailViolation, DEFAULT_POLICY
from hitl import HITLController

logger = logging.getLogger(__name__)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL    = "openai/gpt-oss-20b"

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "navigate_to",
            "description": "Navigate the browser to a specific URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url":    {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["url", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_element",
            "description": "Click an element. Provide multiple locator strategies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "primary_locator":   {"type": "string"},
                    "locator_strategy":  {"type": "string", "enum": ["css","text","aria-label","placeholder","xpath"]},
                    "fallback_locators": {"type": "array", "items": {"type": "object", "properties": {"strategy": {"type": "string"}, "value": {"type": "string"}}}},
                    "description":       {"type": "string"},
                    "is_irreversible":   {"type": "boolean"},
                },
                "required": ["primary_locator", "locator_strategy", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into an input field.",
            "parameters": {
                "type": "object",
                "properties": {
                    "primary_locator":   {"type": "string"},
                    "locator_strategy":  {"type": "string", "enum": ["css","text","aria-label","placeholder","xpath"]},
                    "fallback_locators": {"type": "array", "items": {"type": "object", "properties": {"strategy": {"type": "string"}, "value": {"type": "string"}}}},
                    "value":             {"type": "string"},
                    "is_parameter":      {"type": "boolean"},
                    "parameter_name":    {"type": "string"},
                    "description":       {"type": "string"},
                },
                "required": ["primary_locator", "locator_strategy", "value", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_data",
            "description": "Extract text/data from an element (balance, status, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "primary_locator":   {"type": "string"},
                    "locator_strategy":  {"type": "string", "enum": ["css","xpath","text","aria-label"]},
                    "fallback_locators": {"type": "array", "items": {"type": "object", "properties": {"strategy": {"type": "string"}, "value": {"type": "string"}}}},
                    "output_name":       {"type": "string"},
                    "output_type":       {"type": "string", "enum": ["string","float","int","boolean"]},
                    "description":       {"type": "string"},
                },
                "required": ["primary_locator", "locator_strategy", "output_name", "output_type", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "declare_goal_complete",
            "description": "Call when the goal has been fully achieved.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary":           {"type": "string"},
                    "extracted_outputs": {"type": "object"},
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": "Call when stuck or when something needs human judgment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason":        {"type": "string"},
                    "current_state": {"type": "string"},
                },
                "required": ["reason", "current_state"],
            },
        },
    },
]


def observe_page(page: Page) -> dict:
    state = {"url": page.url, "title": page.title(),
             "timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        snap = page.accessibility.snapshot()
        if snap:
            state["accessibility_tree"] = _condense(snap)
    except Exception:
        pass
    try:
        state["interactive_elements"] = page.evaluate("""() => {
            const els = [];
            ['input','button','select','a[href]','[role=button]'].forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) els.push({
                        tag: el.tagName.toLowerCase(), type: el.type||'',
                        name: el.name||'', id: el.id||'',
                        ariaLabel: el.getAttribute('aria-label')||'',
                        placeholder: el.placeholder||'',
                        text: (el.textContent||el.value||'').trim().slice(0,80),
                    });
                });
            });
            return els.slice(0,25);
        }""")
    except Exception:
        state["interactive_elements"] = []
    try:
        state["modal_dialog"] = page.evaluate("""() => {
            const m = document.querySelector('.modal-overlay');
            return m && m.style.display !== 'none' && m.style.display !== ''
                ? {visible:true, text:m.textContent.trim().slice(0,200)}
                : {visible:false};
        }""")
    except Exception:
        state["modal_dialog"] = {"visible": False}
    return state


def _condense(node: dict, count: list = None) -> dict:
    if count is None: count = [0]
    if count[0] >= 70: return None
    count[0] += 1
    out = {k: str(node[k])[:80] for k in ("role","name","value","description") if node.get(k)}
    ch = [c for child in node.get("children",[]) for c in [_condense(child, count)] if c]
    if ch: out["children"] = ch
    return out or None


def resolve_with_fallbacks(page: Page, primary_strategy: str, primary_value: str, fallbacks: list = None):
    attempts = [(primary_strategy, primary_value)] + [
        (f.get("strategy","css"), f.get("value","")) for f in (fallbacks or [])
    ]
    last_error = None
    for strategy, value in attempts:
        if not value: continue
        try:
            dispatch = {
                "css":         lambda v: page.locator(v).first,
                "text":        lambda v: page.get_by_text(v, exact=False).first,
                "aria-label":  lambda v: page.get_by_label(v).first,
                "placeholder": lambda v: page.get_by_placeholder(v).first,
                "xpath":       lambda v: page.locator(f"xpath={v}").first,
            }
            el = dispatch.get(strategy, lambda v: page.locator(v).first)(value)
            el.wait_for(state="visible", timeout=3000)
            return el, strategy, value
        except Exception as e:
            last_error = e
    raise RuntimeError(f"All locators exhausted. Last: {last_error}. Tried: {attempts}")


def execute_tool(page: Page, name: str, args: dict, guardrails: Guardrails, step_id: str) -> dict:
    result = {"tool": name, "step_id": step_id, "success": False}
    try:
        if name == "navigate_to":
            guardrails.check_url(args["url"])
            page.goto(args["url"], wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(600)
            result.update({"success": True, "url": page.url})

        elif name == "click_element":
            warning = guardrails.check_action("click", not args.get("is_irreversible", False), step_id)
            if warning: result["warning"] = warning
            el, strat, _ = resolve_with_fallbacks(
                page, args["locator_strategy"], args["primary_locator"], args.get("fallback_locators"))
            el.click()
            page.wait_for_timeout(800)
            result.update({"success": True, "strategy_used": strat})

        elif name == "type_text":
            el, strat, _ = resolve_with_fallbacks(
                page, args["locator_strategy"], args["primary_locator"], args.get("fallback_locators"))
            el.fill("")
            el.type(args["value"])
            page.wait_for_timeout(300)
            result.update({"success": True, "strategy_used": strat,
                           "is_parameter": args.get("is_parameter", False),
                           "parameter_name": args.get("parameter_name", "")})

        elif name == "extract_data":
            el, strat, _ = resolve_with_fallbacks(
                page, args["locator_strategy"], args["primary_locator"], args.get("fallback_locators"))
            result.update({"success": True, "output_name": args["output_name"],
                           "raw_value": el.inner_text(), "strategy_used": strat})

        elif name in ("declare_goal_complete", "escalate_to_human"):
            result.update({"success": True, "terminal": True})

        else:
            result["error"] = f"Unknown tool: {name}"

    except GuardrailViolation as e:
        result.update({"error": f"GUARDRAIL: {e}", "blocked": True})
    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"[discovery] {name} failed: {e}")
    return result


class ArtifactBuilder:
    def __init__(self, goal: str, target_url: str):
        self.goal, self.target_url = goal, target_url
        self.steps: list[Step] = []
        self.outputs: list[OutputField] = []
        self.parameters: dict = {}
        self._n = 0

    def _sid(self):
        self._n += 1
        return f"s{self._n}"

    def add_action(self, name: str, args: dict, result: dict):
        if not result.get("success"): return None
        sid = self._sid()
        locs = []
        if args.get("locator_strategy") and args.get("primary_locator"):
            locs.append(Locator(args["locator_strategy"], args["primary_locator"], "primary"))
        for fb in args.get("fallback_locators") or []:
            if fb.get("strategy") and fb.get("value"):
                locs.append(Locator(fb["strategy"], fb["value"], "fallback"))

        action = {"navigate_to":"navigate","click_element":"click",
                  "type_text":"type","extract_data":"extract"}.get(name, name)
        input_var = input_value = None

        if name == "type_text":
            if args.get("is_parameter") and args.get("parameter_name"):
                input_var = args["parameter_name"]
                if input_var not in self.parameters:
                    self.parameters[input_var] = {
                        "type": "string",
                        "description": f"Value for {input_var}",
                        "example": args.get("value", "")
                    }
            else:
                input_value = args.get("value", "")
        elif name == "navigate_to":
            input_value = args.get("url", "")

        if name == "extract_data":
            self.outputs.append(OutputField(
                name=args.get("output_name", f"out_{sid}"),
                locators=locs,
                type=args.get("output_type", "string"),
                transform="text",
                description=args.get("description", ""),
            ))

        step = Step(
            step_id=sid, action=action, locators=locs,
            description=args.get("description", "") or args.get("reason", ""),
            input_var=input_var, input_value=input_value,
            is_reversible=not args.get("is_irreversible", False),
            wait_after_ms=500,
        )
        self.steps.append(step)
        return step

    def build(self, name: str) -> CapabilityArtifact:
        from urllib.parse import urlparse
        domain = urlparse(self.target_url).netloc
        return CapabilityArtifact(
            name=name, version="1.0", description=self.goal,
            target_url=self.target_url, parameters=self.parameters,
            outputs=self.outputs, steps=self.steps,
            safety=SafetyPolicy(
                allowed_domain=domain,
                allowed_paths=["/search", "/member/"],
                has_irreversible_actions=any(not s.is_reversible for s in self.steps),
                irreversible_step_ids=[s.step_id for s in self.steps if not s.is_reversible],
                requires_confirmation=any(not s.is_reversible for s in self.steps),
            ),
            discovered_by="llm-discovery-agent-groq",
            tags=["auto-discovered"],
        )


class DiscoveryAgent:
    def __init__(self, api_key: str, evidence_dir: str = "evidence",
                 headless: bool = True, max_steps: int = 25):
        self.client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL)
        self.evidence_dir = evidence_dir
        self.headless = headless
        self.max_steps = max_steps
        self.guardrails = Guardrails(DEFAULT_POLICY)
        self.hitl = HITLController(evidence_dir=evidence_dir)

    def run(self, goal: str, target_url: str, capability_name: str,
            runtime_params: dict = None) -> dict:
        os.makedirs(self.evidence_dir, exist_ok=True)
        log_path = os.path.join(self.evidence_dir, "discovery_run.log")
        run_log = []

        def log(entry: dict):
            entry["timestamp"] = datetime.now(timezone.utc).isoformat()
            run_log.append(entry)
            logger.info(f"[discovery] {json.dumps(entry)}")

        log({"event": "discovery_start", "goal": goal, "target": target_url, "llm": GROQ_MODEL})
        builder = ArtifactBuilder(goal=goal, target_url=target_url)

        system_prompt = f"""You are a browser automation agent. Accomplish the goal by calling tools.

GOAL: {goal}
TARGET: {target_url}

Rules:
1. Prefer aria-label and placeholder locators over CSS.
2. Always provide fallback_locators for click and type actions.
3. Mark type_text as is_parameter=true when the value changes per invocation, set parameter_name.
4. If you see "not found" or "no record" after searching — call declare_goal_complete with that note.
5. If stuck after 2 attempts, call escalate_to_human.
6. Call declare_goal_complete as soon as goal is achieved.
7. Only navigate within: {self.guardrails.policy.allowed_domains}
"""
        messages = [{"role": "system", "content": system_prompt}]

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
            step_count = 0
            goal_complete = False
            consecutive_failures = 0

            try:
                while step_count < self.max_steps and not goal_complete:
                    step_count += 1
                    page_state = observe_page(page)
                    log({"event": "observed", "step": step_count, "url": page_state["url"]})

                    messages.append({
                        "role": "user",
                        "content": f"Page state:\n{json.dumps(page_state, indent=2)}\n\nWhat next to achieve the goal?"
                    })

                    response = self.client.chat.completions.create(
                        model=GROQ_MODEL,
                        messages=messages,
                        tools=AGENT_TOOLS,
                        tool_choice="auto",
                        max_tokens=1500,
                        temperature=0.1,
                    )

                    msg = response.choices[0].message
                    log({"event": "llm_response",
                         "finish_reason": response.choices[0].finish_reason,
                         "tool_calls": len(msg.tool_calls or [])})
                    messages.append(msg)

                    if not msg.tool_calls:
                        if msg.content:
                            log({"event": "llm_text", "content": msg.content[:200]})
                        continue

                    tool_results = []
                    for tc in msg.tool_calls:
                        name = tc.function.name
                        try:
                            args = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            args = {}

                        log({"event": "tool_call", "tool": name, "args": args})

                        if name == "declare_goal_complete":
                            log({"event": "goal_complete",
                                 "summary": args.get("summary"),
                                 "outputs": args.get("extracted_outputs", {})})
                            goal_complete = True
                            tool_results.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": "Goal complete."
                            })
                            break

                        if name == "escalate_to_human":
                            log({"event": "hitl_escalation", "reason": args.get("reason")})
                            hitl_result = self.hitl.escalate(
                                page=page, capability_name=capability_name, goal=goal,
                                current_step_id=f"discovery_step_{step_count}",
                                current_step_description=args.get("current_state", ""),
                                reason=args.get("reason", "Agent stuck"),
                                non_interactive=True,
                            )
                            tool_results.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": f"Human resolved. URL: {hitl_result['url_after']}"
                            })
                            consecutive_failures = 0
                            continue

                        result = execute_tool(page, name, args, self.guardrails, f"s{step_count}")
                        log({"event": "tool_result", "tool": name,
                             "success": result.get("success"),
                             "error": result.get("error", "")})

                        if result.get("success"):
                            builder.add_action(name, args, result)
                            consecutive_failures = 0
                        else:
                            consecutive_failures += 1
                            if self.hitl.detect_stuck(consecutive_failures, result.get("error", "")):
                                self.hitl.escalate(
                                    page=page, capability_name=capability_name, goal=goal,
                                    current_step_id=f"discovery_step_{step_count}",
                                    current_step_description=name,
                                    reason=f"Stuck: {result.get('error')}",
                                    non_interactive=True,
                                )
                                consecutive_failures = 0

                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result)
                        })

                    messages.extend(tool_results)

            except Exception as e:
                log({"event": "discovery_error", "error": str(e),
                     "trace": traceback.format_exc()})
            finally:
                try:
                    ss = os.path.join(self.evidence_dir, "screenshots", "discovery_final.png")
                    os.makedirs(os.path.dirname(ss), exist_ok=True)
                    page.screenshot(path=ss)
                    log({"event": "final_screenshot", "path": ss})
                except Exception:
                    pass
                browser.close()

        artifact = builder.build(name=capability_name)
        artifact.save(os.path.join(self.evidence_dir, "capability_artifact.json"))
        with open(log_path, "w") as f:
            for e in run_log:
                f.write(json.dumps(e) + "\n")

        return {
            "success": goal_complete,
            "artifact": artifact,
            "artifact_path": os.path.join(self.evidence_dir, "capability_artifact.json"),
            "log_path": log_path,
            "steps_taken": step_count,
        }