"""Adaptive Cards rendered with the Microsoft Teams SDK."""

from microsoft_teams.cards import AdaptiveCard

from .contracts import ApprovalPrompt


def approval_card(prompt: ApprovalPrompt) -> AdaptiveCard:
    body: list[dict[str, object]] = [
        {
            "type": "TextBlock",
            "text": "🔐 Tool approval required",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "FactSet",
            "facts": [{"title": "Tool", "value": prompt.tool_name}],
        },
    ]
    if prompt.tool_input:
        body.append(
            {
                "type": "TextBlock",
                "text": prompt.tool_input,
                "wrap": True,
                "fontType": "Monospace",
                "isSubtle": True,
            }
        )

    decision = {
        "s": prompt.metadata.session_id,
        "t": prompt.tool_id,
        "n": prompt.tool_name,
    }
    return AdaptiveCard.model_validate(
        {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "body": body,
            "actions": [
                {"type": "Action.Submit", "title": "✅ Approve", "data": {**decision, "a": True}},
                {"type": "Action.Submit", "title": "❌ Deny", "data": {**decision, "a": False}},
            ],
        }
    )

