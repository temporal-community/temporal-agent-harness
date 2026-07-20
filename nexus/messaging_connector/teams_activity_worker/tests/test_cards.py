from teams_activity_worker.cards import approval_card
from teams_activity_worker.contracts import ApprovalPrompt, TextMetadata


def test_approval_card_contains_compact_webhook_payloads() -> None:
    prompt = ApprovalPrompt(
        metadata=TextMetadata("user", "teams:conversation-1", "", "", "", "msteams"),
        tool_id="tool-1",
        tool_name="deploy",
        tool_input='{"environment":"prod"}',
    )

    card = approval_card(prompt).model_dump(by_alias=True, exclude_none=True)

    assert card["version"] == "1.4"
    assert card["body"][2]["fontType"] == "Monospace"
    assert card["actions"][0]["data"] == {
        "s": "teams:conversation-1",
        "t": "tool-1",
        "n": "deploy",
        "a": True,
    }
    assert card["actions"][1]["data"]["a"] is False

