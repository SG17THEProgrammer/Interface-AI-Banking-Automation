"""
capabilities/check_balance.py
-------------------------------
Pre-built artifact: look up a member and return balance + status.
"""

from artifact import (
    CapabilityArtifact, Step, Locator, Checkpoint,
    OutputField, SafetyPolicy,
)


def build(target_url: str = "http://localhost:8080") -> CapabilityArtifact:
    return CapabilityArtifact(
        name="check_member_balance",
        version="1.1",
        description=(
            "Look up a bank member by ID and return their current account "
            "balance and account status."
        ),
        target_url=f"{target_url}/search",
        parameters={
            "member_id": {
                "type": "string",
                "description": "Six-digit member ID (e.g. '100001')",
                "example": "100001",
            }
        },
        outputs=[
            OutputField(
                name="balance",
                locators=[
                    Locator("css",   "#balance-amount",                         "Balance span by id"),
                    Locator("xpath", "//span[contains(@class,'balance')]",       "Balance by class"),
                ],
                type="float",
                transform="text",
                description="Current account balance as a float",
            ),
            OutputField(
                name="account_status",
                locators=[
                    Locator("css",   "#account-status",                          "Status span by id"),
                    Locator("xpath", "//span[contains(@class,'status-')]",       "Status by class"),
                ],
                type="string",
                transform="text",
                description="Account status: Active | Frozen | Closed",
            ),
            OutputField(
                name="member_name",
                locators=[
                    Locator("xpath", "//td[text()='Full Name']/following-sibling::td",
                            "Table cell after Full Name label"),
                ],
                type="string",
                transform="text",
                description="Member full name",
            ),
        ],
        steps=[
            Step(
                step_id="s1_navigate",
                action="navigate",
                locators=[],
                description="Navigate to member search page",
                input_value=f"{target_url}/search",
                checkpoint=Checkpoint("url_contains", "/search"),
            ),
            Step(
                step_id="s2_type_id",
                action="type",
                locators=[
                    Locator("aria-label",  "Member ID",                  "Stable aria-label"),
                    Locator("placeholder", "Enter 6-digit Member ID",    "Placeholder fallback"),
                    Locator("css",         "input[name='member_id']",    "Name attribute fallback"),
                    Locator("css",         "input[type='text']:first-of-type", "Last resort"),
                ],
                description="Type the member ID into the search box",
                input_var="member_id",
                wait_after_ms=300,
            ),
            Step(
                step_id="s3_click_search",
                action="click",
                locators=[
                    Locator("css",        "input[type='submit'][value='Search']", "Submit by value"),
                    Locator("aria-label", "Search",                               "Aria-label fallback"),
                    Locator("text",       "Search",                               "Text fallback"),
                ],
                description="Click the Search button",
                checkpoint=Checkpoint("url_contains", "/member/"),
                wait_after_ms=800,
            ),
            Step(
                step_id="s4_assert_result",
                action="assert",
                locators=[
                    Locator("css", "#balance-amount, #member-not-found",
                            "Either result must be visible"),
                ],
                description="Assert result page loaded (balance or not-found)",
                checkpoint=Checkpoint("element_visible", "#balance-amount, #member-not-found"),
            ),
        ],
        safety=SafetyPolicy(
            allowed_domain="localhost:8080",
            allowed_paths=["/search", "/member/"],
            has_irreversible_actions=False,
            pii_fields=["member_name"],
        ),
        tags=["read-only", "balance-lookup"],
    )
