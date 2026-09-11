"""
Capability Artifact Schema
--------------------------
This is the central data contract of the entire system.
A capability artifact is a typed, versioned, serializable description of a
reusable automation flow. It is what the Discovery agent produces and what
the Replay engine consumes.

Design decisions:
- Each step carries multiple locator strategies (ordered by robustness) so
  replay can fall back gracefully when the primary strategy breaks.
- Outputs are declared with type + extractor so the Replay engine knows
  exactly what to scrape and how to return it.
- Safety metadata is explicit: the artifact itself declares whether it
  contains irreversible actions and what the allowed domain is.
- Version field enables drift detection across tenant deployments.
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Locator Strategies
# ---------------------------------------------------------------------------

LOCATOR_STRATEGIES = [
    "aria-label",       # Most stable — survives markup changes
    "aria-role+text",   # Role + visible text — second most stable
    "text",             # Visible text match
    "placeholder",      # Input placeholder text
    "css",              # CSS selector — use as last resort
    "xpath",            # XPath — final fallback
]


@dataclass
class Locator:
    """
    A single element-targeting strategy.
    Multiple Locators are chained per step; the engine tries them in order.
    """
    strategy: str          # One of LOCATOR_STRATEGIES
    value: str             # The selector / label / text to match
    description: str = ""  # Human note: why this locator was chosen


@dataclass
class Checkpoint:
    """
    An assertion the Replay engine makes to confirm a step succeeded.
    Distinguishes success from silently failing automation.
    """
    type: str    # "url_contains" | "element_visible" | "element_text_contains" | "page_title_contains"
    value: str   # Expected value to match


@dataclass
class Step:
    """
    One atomic action in the automation flow.
    The Replay engine executes steps in order, applying the locator chain
    and verifying the checkpoint after each action.
    """
    step_id: str
    action: str            # "click" | "type" | "wait" | "extract" | "navigate" | "assert"
    locators: list[Locator]
    description: str = ""
    input_var: Optional[str] = None     # If set, value comes from runtime parameters
    input_value: Optional[str] = None   # Literal value (used when input_var is None)
    checkpoint: Optional[Checkpoint] = None
    wait_after_ms: int = 500            # ms to wait after action before proceeding
    is_reversible: bool = True          # False = risky action requiring special handling


@dataclass
class OutputField:
    """
    Declares one piece of data the flow extracts and returns.
    """
    name: str
    locators: list[Locator]
    type: str              # "string" | "float" | "int" | "boolean"
    transform: str = "text"   # "text" | "inner_html" | "attribute:href"
    description: str = ""


@dataclass
class SafetyPolicy:
    """
    Safety metadata baked into the artifact itself.
    The Replay engine and guardrail layer both consult this.
    """
    allowed_domain: str
    allowed_paths: list[str]       # URL path prefixes that are permitted
    has_irreversible_actions: bool = False
    irreversible_step_ids: list[str] = field(default_factory=list)
    requires_confirmation: bool = False
    pii_fields: list[str] = field(default_factory=list)  # Field names to redact in logs


@dataclass
class CapabilityArtifact:
    """
    The top-level artifact. This is what gets saved to /evidence/ and
    loaded by the Replay engine.

    Schema decisions:
    - parameters: typed input contract so a calling agent knows what to supply
    - outputs: typed output contract so a calling agent knows what it gets back
    - steps: ordered, with multi-strategy locators and checkpoints
    - safety: explicit policy attached to the artifact, not assumed
    - version + created_at: enables drift detection and audit
    """
    name: str
    version: str
    description: str
    target_url: str
    parameters: dict[str, dict]   # {"member_id": {"type": "string", "description": "..."}}
    outputs: list[OutputField]
    steps: list[Step]
    safety: SafetyPolicy
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    discovered_by: str = "llm-discovery-agent"
    tags: list[str] = field(default_factory=list)

    # -----------------------------------------------------------------------
    # Serialization
    # -----------------------------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(self.to_json())

    @classmethod
    def from_dict(cls, data: dict) -> "CapabilityArtifact":
        # Reconstruct nested dataclasses
        outputs = [
            OutputField(
                name=o["name"],
                locators=[Locator(**loc) for loc in o["locators"]],
                type=o["type"],
                transform=o.get("transform", "text"),
                description=o.get("description", ""),
            )
            for o in data.get("outputs", [])
        ]
        steps = [
            Step(
                step_id=s["step_id"],
                action=s["action"],
                locators=[Locator(**loc) for loc in s.get("locators", [])],
                description=s.get("description", ""),
                input_var=s.get("input_var"),
                input_value=s.get("input_value"),
                checkpoint=Checkpoint(**s["checkpoint"]) if s.get("checkpoint") else None,
                wait_after_ms=s.get("wait_after_ms", 500),
                is_reversible=s.get("is_reversible", True),
            )
            for s in data.get("steps", [])
        ]
        safety_data = data.get("safety", {})
        safety = SafetyPolicy(
            allowed_domain=safety_data.get("allowed_domain", ""),
            allowed_paths=safety_data.get("allowed_paths", []),
            has_irreversible_actions=safety_data.get("has_irreversible_actions", False),
            irreversible_step_ids=safety_data.get("irreversible_step_ids", []),
            requires_confirmation=safety_data.get("requires_confirmation", False),
            pii_fields=safety_data.get("pii_fields", []),
        )
        return cls(
            name=data["name"],
            version=data["version"],
            description=data["description"],
            target_url=data["target_url"],
            parameters=data.get("parameters", {}),
            outputs=outputs,
            steps=steps,
            safety=safety,
            created_at=data.get("created_at", ""),
            discovered_by=data.get("discovered_by", ""),
            tags=data.get("tags", []),
        )

    @classmethod
    def load(cls, path: str) -> "CapabilityArtifact":
        with open(path) as f:
            return cls.from_dict(json.load(f))


# ---------------------------------------------------------------------------
# Pre-built artifact: check_member_balance
# Used as a reference / fallback when discovery hasn't been run yet
# ---------------------------------------------------------------------------

def build_check_balance_artifact(target_url: str = "http://localhost:8080") -> CapabilityArtifact:
    """
    Returns a hand-authored artifact for the check_member_balance capability.
    This is the "ground truth" schema that the discovery agent should produce
    autonomously. Kept here for testing replay independently of discovery.
    """
    return CapabilityArtifact(
        name="check_member_balance",
        version="1.0",
        description=(
            "Look up a bank member by ID and return their current account balance "
            "and account status. Returns a business outcome if the member is not found."
        ),
        target_url=f"{target_url}/search",
        parameters={
            "member_id": {
                "type": "string",
                "description": "Six-digit member ID number (e.g. '100001')",
                "example": "100001",
            }
        },
        outputs=[
            OutputField(
                name="balance",
                locators=[
                    Locator(strategy="css", value="#balance-amount", description="Balance span with id"),
                    Locator(strategy="xpath", value="//span[contains(@class,'balance')]", description="Balance by class fallback"),
                ],
                type="float",
                transform="text",
                description="Current account balance as a float (e.g. 4521.00)",
            ),
            OutputField(
                name="account_status",
                locators=[
                    Locator(strategy="css", value="#account-status", description="Status span with id"),
                    Locator(strategy="xpath", value="//span[contains(@class,'status-')]", description="Status by class fallback"),
                ],
                type="string",
                transform="text",
                description="Account status: Active | Frozen | Closed",
            ),
            OutputField(
                name="member_name",
                locators=[
                    Locator(strategy="xpath", value="//td[text()='Full Name']/following-sibling::td", description="Table cell next to Full Name label"),
                ],
                type="string",
                transform="text",
                description="Member's full name",
            ),
        ],
        steps=[
            Step(
                step_id="s1_navigate_to_search",
                action="navigate",
                locators=[],
                description="Navigate to the member search page",
                input_value=f"{target_url}/search",
                checkpoint=Checkpoint(type="url_contains", value="/search"),
                wait_after_ms=500,
            ),
            Step(
                step_id="s2_type_member_id",
                action="type",
                locators=[
                    Locator(strategy="aria-label", value="Member ID", description="Input has aria-label Member ID"),
                    Locator(strategy="placeholder", value="Enter 6-digit Member ID", description="Placeholder text fallback"),
                    Locator(strategy="css", value="input[name='member_id']", description="Name attribute fallback"),
                    Locator(strategy="css", value="input[type='text']:first-of-type", description="First text input last resort"),
                ],
                description="Type the member ID into the search box",
                input_var="member_id",
                checkpoint=None,
                wait_after_ms=300,
            ),
            Step(
                step_id="s3_click_search",
                action="click",
                locators=[
                    Locator(strategy="css", value="input[type='submit'][value='Search']", description="Submit button by value"),
                    Locator(strategy="text", value="Search", description="Button by text fallback"),
                    Locator(strategy="aria-label", value="Search", description="Button by aria-label"),
                ],
                description="Click the Search button to submit the form",
                checkpoint=Checkpoint(type="url_contains", value="/member/"),
                wait_after_ms=800,
            ),
            Step(
                step_id="s4_assert_page_loaded",
                action="assert",
                locators=[
                    Locator(strategy="css", value="#balance-amount, #member-not-found", description="Either balance amount or not-found message must exist"),
                ],
                description="Assert either the balance or the not-found message is visible",
                checkpoint=Checkpoint(type="element_visible", value="#balance-amount, #member-not-found"),
                wait_after_ms=200,
            ),
        ],
        safety=SafetyPolicy(
            allowed_domain="localhost:8080",
            allowed_paths=["/search", "/member/"],
            has_irreversible_actions=False,
            irreversible_step_ids=[],
            requires_confirmation=False,
            pii_fields=["member_name"],
        ),
        tags=["read-only", "balance-lookup", "member-services"],
    )
