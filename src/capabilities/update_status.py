"""
capabilities/update_status.py
-------------------------------
Pre-built artifact: change the account status of a member.
This flow includes an irreversible action (the modal confirmation).
"""

from artifact import (
    CapabilityArtifact, Step, Locator, Checkpoint,
    OutputField, SafetyPolicy,
)


def build(target_url: str = "http://localhost:8080") -> CapabilityArtifact:
    return CapabilityArtifact(
        name="update_account_status",
        version="1.1",
        description=(
            "Navigate to a member's update page, set the new account status, "
            "confirm the modal dialog, and return the updated status."
        ),
        target_url=f"{target_url}/search",
        parameters={
            "member_id": {
                "type": "string",
                "description": "Six-digit member ID",
                "example": "100001",
            },
            "new_status": {
                "type": "string",
                "description": "New account status: Active | Frozen | Closed",
                "example": "Frozen",
            },
            "reason_code": {
                "type": "string",
                "description": "Reason code for the audit log",
                "example": "RISK-001",
            },
        },
        outputs=[
            OutputField(
                name="updated_status",
                locators=[
                    Locator("css",   "#new-account-status",                        "New status span"),
                    Locator("xpath", "//span[contains(@class,'status-')]",         "Status by class"),
                ],
                type="string",
                transform="text",
                description="Confirmed new account status after update",
            ),
            OutputField(
                name="member_name",
                locators=[
                    Locator("xpath", "//td[text()='Full Name']/following-sibling::td",
                            "Full Name table cell"),
                ],
                type="string",
                transform="text",
                description="Member name for confirmation",
            ),
        ],
        steps=[
            # ── Navigate directly to the member's update page ──────────────
            # The chat_app._patch_update_url() replaces {member_id} at runtime.
            Step(
                step_id="s1_navigate_update",
                action="navigate",
                locators=[],
                description="Navigate directly to member update page",
                input_value=f"{target_url}/member/{{member_id}}/update",
                checkpoint=Checkpoint("url_contains", "/update"),
                wait_after_ms=600,
            ),
            # ── Select new status ──────────────────────────────────────────
            Step(
                step_id="s2_select_status",
                action="select",
                locators=[
                    Locator("css",   "select[name='new_status']", "Status dropdown by name"),
                    Locator("xpath", "//select[@name='new_status']", "XPath fallback"),
                ],
                description="Select the new account status from dropdown",
                input_var="new_status",
                wait_after_ms=300,
            ),
            # ── Fill reason code ───────────────────────────────────────────
            Step(
                step_id="s3_type_reason",
                action="type",
                locators=[
                    Locator("aria-label", "Reason code",              "Aria-label"),
                    Locator("css",        "input[name='reason']",     "Name attribute"),
                    Locator("placeholder","Enter reason code",         "Placeholder"),
                ],
                description="Type the reason code",
                input_var="reason_code",
                wait_after_ms=200,
            ),
            # ── Click Update Account button (opens modal) ──────────────────
            Step(
                step_id="s4_click_update",
                action="click",
                locators=[
                    Locator("aria-label", "Update Account",          "Aria-label on button"),
                    Locator("css",        "button.btn-danger",        "Danger-class button"),
                    Locator("text",       "Update Account",           "Text content"),
                ],
                description="Click Update Account to open the confirmation modal",
                is_reversible=False,
                wait_after_ms=600,
                checkpoint=Checkpoint("element_visible", "#confirmModal"),
            ),
            # ── Confirm in modal ───────────────────────────────────────────
            Step(
                step_id="s5_confirm_modal",
                action="click",
                locators=[
                    Locator("aria-label", "Yes, confirm update",     "Modal confirm button"),
                    Locator("css",        "button.btn-danger[type='submit']", "Submit in modal"),
                    Locator("text",       "Yes, Update",             "Text fallback"),
                ],
                description="Confirm the update in the modal dialog",
                is_reversible=False,
                wait_after_ms=1000,
                checkpoint=Checkpoint("element_visible", "#update-success-message, #update-error-message"),
            ),
            # ── Assert success ─────────────────────────────────────────────
            Step(
                step_id="s6_assert_success",
                action="assert",
                locators=[
                    Locator("css", "#update-success-message", "Success confirmation element"),
                ],
                description="Assert the update success message is visible",
                checkpoint=Checkpoint("element_visible", "#update-success-message"),
            ),
        ],
        safety=SafetyPolicy(
            allowed_domain="localhost:8080",
            allowed_paths=["/search", "/member/"],
            has_irreversible_actions=True,
            irreversible_step_ids=["s4_click_update", "s5_confirm_modal"],
            requires_confirmation=True,
            pii_fields=["member_name"],
        ),
        tags=["write", "account-management"],
    )
