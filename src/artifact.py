"""
Capability Artifact Schema
--------------------------
Central data contract — versioned, typed, engine-executable JSON document.
The Discovery agent produces it; the Replay engine consumes it.
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
from datetime import datetime, timezone

LOCATOR_STRATEGIES = [
    "aria-label", "aria-role+text", "text", "placeholder", "css", "xpath",
]


@dataclass
class Locator:
    strategy: str
    value: str
    description: str = ""


@dataclass
class Checkpoint:
    type: str   # "url_contains" | "element_visible" | "element_text_contains" | "page_title_contains"
    value: str


@dataclass
class Step:
    step_id: str
    action: str         # "click" | "type" | "select" | "wait" | "extract" | "navigate" | "assert"
    locators: list[Locator]
    description: str = ""
    input_var: Optional[str] = None
    input_value: Optional[str] = None
    checkpoint: Optional[Checkpoint] = None
    wait_after_ms: int = 500
    is_reversible: bool = True


@dataclass
class OutputField:
    name: str
    locators: list[Locator]
    type: str               # "string" | "float" | "int" | "boolean"
    transform: str = "text"
    description: str = ""


@dataclass
class SafetyPolicy:
    allowed_domain: str
    allowed_paths: list[str]
    has_irreversible_actions: bool = False
    irreversible_step_ids: list[str] = field(default_factory=list)
    requires_confirmation: bool = False
    pii_fields: list[str] = field(default_factory=list)


@dataclass
class CapabilityArtifact:
    name: str
    version: str
    description: str
    target_url: str
    parameters: dict[str, dict]
    outputs: list[OutputField]
    steps: list[Step]
    safety: SafetyPolicy
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    discovered_by: str = "hand-authored"
    tags: list[str] = field(default_factory=list)

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
