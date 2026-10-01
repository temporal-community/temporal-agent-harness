# ABOUTME: Reading a Dogwood policy's rules the way Dogwood numbers them, with the annotations
# Playpen gives meaning to. No CLI needed.

from __future__ import annotations

import pytest

from temporal_agent_harness.playpen.policy_file import parse_rules

from .conftest import POLICY_STORE


def test_macros_are_not_rules_and_annotations_are_read():
    rules = parse_rules(
        """
        // a comment with a ; and a "quote
        def temporal recently(?w, ?body) { formerly within ?w ?body };

        @id("a") @reason("Says \\"hi\\"; politely.")
        permit (principal, action == NS::Action::"greet", resource);

        @id("b")
        @on_deny("escalate")
        forbid (principal, action in [NS::Action::"x", NS::Action::"y"], resource)
        when { context.input.note == "a ; inside a string" };

        permit (principal, action, resource);
        """
    )
    assert [(r.index, r.effect, r.id) for r in rules] == [
        (0, "permit", "a"),
        (1, "forbid", "b"),
        (2, "permit", "rule 2"),
    ]
    assert rules[0].annotations["reason"] == 'Says "hi"; politely.'
    assert rules[0].actions == {"greet"}
    assert rules[1].escalates_on_deny and rules[1].actions == {"x", "y"}
    assert rules[2].covers("anything")


def test_an_unknown_on_deny_value_is_an_error():
    with pytest.raises(ValueError, match="on_deny"):
        parse_rules('@on_deny("escalte") forbid (principal, action, resource);')


def test_an_unterminated_statement_is_an_error():
    with pytest.raises(ValueError):
        parse_rules("permit (principal, action, resource)")


def test_the_example_policy_parses():
    rules = parse_rules((POLICY_STORE / "support" / "policy.dw").read_text())
    assert [r.id for r in rules] == [
        "lookups",
        "email_address_on_file",
        "refund_up_to_order_total",
        "one_refund_per_order",
        "large_refund_needs_a_person",
    ]
    assert [r.id for r in rules if r.escalates_on_deny] == ["large_refund_needs_a_person"]
    assert all("reason" in r.annotations for r in rules)
