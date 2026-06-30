# ABOUTME: Reusable workflow-side slash/operator command definitions for the harness.
# Commands bundle discovery metadata with deterministic in-workflow execution.

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from temporal_agent_harness.harness.agent_protocol import (
    AgentStatus,
    OperatorCommand,
    OperatorCommandArgument,
    SlashCommand,
    TextReply,
    ToolApprovalPolicy,
)

APPROVAL_MODE_CHOICES = ("strict", "safe", "skip")
DEFAULT_COMMAND_NAMES = ("approvals", "allow-tools", "status", "stop")


@dataclass(frozen=True)
class SlashCommandContext:
    """Workflow-side state and mutators available to slash command handlers."""

    current_status: AgentStatus
    current_approval_policy: ToolApprovalPolicy
    set_approval_policy: Callable[[ToolApprovalPolicy], None]
    close: Callable[[], None]


SlashCommandHandler = Callable[[SlashCommandContext, SlashCommand], TextReply]


@dataclass(frozen=True)
class SlashCommandDefinition:
    """A slash/operator command: UI metadata plus its workflow-side handler."""

    command: OperatorCommand
    handler: SlashCommandHandler

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "command", OperatorCommand.model_validate(self.command)
        )

    def matches(self, name: str) -> bool:
        return (
            name == self.command.name
            or name == self.command.payload_name
            or name in self.command.aliases
        )

    def execute(
        self, context: SlashCommandContext, command: SlashCommand
    ) -> TextReply:
        return self.handler(context, command)


def enum_arg(
    choices: Iterable[str],
    *,
    placeholder: str | None = None,
    required: bool = True,
) -> OperatorCommandArgument:
    return OperatorCommandArgument(
        kind="enum",
        required=required,
        choices=tuple(choices),
        placeholder=placeholder,
    )


def text_arg(
    *,
    placeholder: str | None = None,
    required: bool = True,
) -> OperatorCommandArgument:
    return OperatorCommandArgument(
        kind="text",
        required=required,
        placeholder=placeholder,
    )


def tool_names_arg(
    *,
    placeholder: str | None = "tool_name",
    required: bool = True,
    allow_multiple: bool = True,
) -> OperatorCommandArgument:
    return OperatorCommandArgument(
        kind="tool_names",
        required=required,
        placeholder=placeholder,
        allow_multiple=allow_multiple,
    )


def command(
    *,
    name: str,
    payload_name: str,
    label: str,
    description: str,
    handler: SlashCommandHandler,
    aliases: Iterable[str] = (),
    argument: OperatorCommandArgument | None = None,
    source: Literal["harness", "agent"] = "agent",
) -> SlashCommandDefinition:
    return SlashCommandDefinition(
        command=OperatorCommand(
            name=name,
            payload_name=payload_name,
            label=label,
            description=description,
            aliases=tuple(aliases),
            argument=argument,
            source=source,
        ),
        handler=handler,
    )


def approvals() -> SlashCommandDefinition:
    return command(
        name="approvals",
        payload_name="set-approvals",
        label="/approvals",
        description="Set the tool approval policy for this session.",
        aliases=("set-approvals",),
        argument=enum_arg(
            APPROVAL_MODE_CHOICES,
            placeholder="strict | safe | skip",
        ),
        source="harness",
        handler=_handle_approvals,
    )


def allow_tools() -> SlashCommandDefinition:
    return command(
        name="allow-tools",
        payload_name="allow-tools",
        label="/allow-tools",
        description="Auto-approve one or more named tools for this session.",
        aliases=("allow-tool",),
        argument=tool_names_arg(),
        source="harness",
        handler=_handle_allow_tools,
    )


def status() -> SlashCommandDefinition:
    return command(
        name="status",
        payload_name="status",
        label="/status",
        description="Show the current harness status for this session.",
        source="harness",
        handler=_handle_status,
    )


def stop() -> SlashCommandDefinition:
    return command(
        name="stop",
        payload_name="stop-agent",
        label="/stop",
        description="Stop this agent workflow.",
        aliases=("stop-agent",),
        source="harness",
        handler=_handle_stop,
    )


def model_selector(
    *,
    choices: Iterable[str],
    set_model: Callable[[str], None],
    name: str = "model",
    payload_name: str = "set-model",
    label: str = "/model",
    description: str = "Set the model for this session.",
    placeholder: str = "model",
    source: Literal["harness", "agent"] = "agent",
) -> SlashCommandDefinition:
    model_choices = tuple(choices)

    def handle(
        _context: SlashCommandContext, slash_command: SlashCommand
    ) -> TextReply:
        selected = _normalize_slash_arg(slash_command.arg)
        if selected not in model_choices:
            return TextReply(
                text=f"Choose one of: {_format_inline_code(model_choices)}."
            )
        set_model(selected)
        return TextReply(text=f"Model set to **{selected}**.")

    return command(
        name=name,
        payload_name=payload_name,
        label=label,
        description=description,
        argument=enum_arg(model_choices, placeholder=placeholder),
        source=source,
        handler=handle,
    )


def commands(*names: str) -> tuple[SlashCommandDefinition, ...]:
    selected = names or DEFAULT_COMMAND_NAMES
    definitions: list[SlashCommandDefinition] = []
    seen: set[str] = set()
    for name in selected:
        canonical = _canonical_command_name(name)
        if canonical in seen:
            continue
        seen.add(canonical)
        definitions.append(_BUILTIN_COMMAND_FACTORIES[canonical]())
    return tuple(definitions)


def default_commands() -> tuple[SlashCommandDefinition, ...]:
    return commands(*DEFAULT_COMMAND_NAMES)


def _canonical_command_name(name: str) -> str:
    normalized = name.strip().removeprefix("/")
    aliases = {
        "set-approvals": "approvals",
        "allow-tool": "allow-tools",
        "stop-agent": "stop",
    }
    canonical = aliases.get(normalized, normalized)
    if canonical not in _BUILTIN_COMMAND_FACTORIES:
        choices = ", ".join(DEFAULT_COMMAND_NAMES)
        raise ValueError(
            f"unknown packaged slash command {name!r}; choose one of: {choices}"
        )
    return canonical


def _normalize_slash_arg(arg: str | None) -> str:
    return (arg or "").strip()


def _approval_policy_for_mode(mode: str | None) -> ToolApprovalPolicy | None:
    match _normalize_slash_arg(mode).lower():
        case "strict":
            return ToolApprovalPolicy.always_require_approvals()
        case "safe":
            return ToolApprovalPolicy.allow_inherently_safe()
        case "skip":
            return ToolApprovalPolicy.dangerously_skip_all()
        case _:
            return None


def _approval_policy_label(policy: ToolApprovalPolicy) -> str:
    if policy.dangerously_skip_all_approvals:
        return "skip"
    base = "safe" if policy.auto_approve_inherently_safe else "strict"
    if policy.auto_approve_tools:
        return f"{base} + allow-list"
    return base


def _format_inline_code(values: Iterable[str]) -> str:
    return ", ".join(f"`{value}`" for value in values)


def _parse_tool_names(arg: str | None) -> tuple[str, ...]:
    normalized = _normalize_slash_arg(arg).replace(",", " ")
    return tuple(part for part in normalized.split() if part)


def _render_harness_status(status: AgentStatus) -> str:
    allowed = tuple(sorted(status.approval_policy.auto_approve_tools))
    pending_approvals = sorted(
        {
            f"`{approval.tool_name}` (turn {approval.turn_number})"
            for approval in status.pending_approvals
        }
    )
    if status.subagents:
        subagents = ", ".join(
            f"`{item.subagent_id}` ({item.agent_key}, next turn {item.next_expected_turn})"
            for item in status.subagents
        )
    else:
        subagents = "none"

    lines = [
        f"- Agent id: `{status.agent_id}`",
        f"- Turn: `{status.current_turn}` ({'active' if status.turn_active else 'idle'})",
        f"- Queued turns: `{len(status.pending_turns)}`",
        f"- Message queueing: `{'on' if status.is_message_queuing_enabled else 'off'}`",
        f"- Approvals: `{_approval_policy_label(status.approval_policy)}`",
        f"- Auto-approved tools: {_format_inline_code(allowed) if allowed else 'none'}",
        f"- Pending approvals: {', '.join(pending_approvals) if pending_approvals else 'none'}",
        f"- Active subagents: {subagents}",
    ]
    return "\n".join(lines)


def _handle_approvals(
    context: SlashCommandContext, slash_command: SlashCommand
) -> TextReply:
    selected = _normalize_slash_arg(slash_command.arg).lower()
    policy = _approval_policy_for_mode(slash_command.arg)
    if policy is None:
        return TextReply(
            text=f"Choose one of: {_format_inline_code(APPROVAL_MODE_CHOICES)}."
        )
    context.set_approval_policy(policy)
    return TextReply(text=f"Approvals set to **{selected}**.")


def _handle_allow_tools(
    context: SlashCommandContext, slash_command: SlashCommand
) -> TextReply:
    tool_names = _parse_tool_names(slash_command.arg)
    if not tool_names:
        return TextReply(
            text="Choose one or more tool names to auto-approve for this session."
        )
    policy = context.current_approval_policy
    for tool_name in tool_names:
        policy = policy.with_tool_allowed(tool_name)
    context.set_approval_policy(policy)
    noun = "Tool" if len(tool_names) == 1 else "Tools"
    return TextReply(
        text=f"{noun} {_format_inline_code(tool_names)} will be auto-approved."
    )


def _handle_status(
    context: SlashCommandContext, _slash_command: SlashCommand
) -> TextReply:
    return TextReply(text=_render_harness_status(context.current_status))


def _handle_stop(
    context: SlashCommandContext, _slash_command: SlashCommand
) -> TextReply:
    context.close()
    return TextReply(text="Agent stop requested.")


_BUILTIN_COMMAND_FACTORIES: dict[str, Callable[[], SlashCommandDefinition]] = {
    "approvals": approvals,
    "allow-tools": allow_tools,
    "status": status,
    "stop": stop,
}


__all__ = [
    "APPROVAL_MODE_CHOICES",
    "DEFAULT_COMMAND_NAMES",
    "SlashCommandContext",
    "SlashCommandDefinition",
    "SlashCommandHandler",
    "allow_tools",
    "approvals",
    "command",
    "commands",
    "default_commands",
    "enum_arg",
    "model_selector",
    "status",
    "stop",
    "text_arg",
    "tool_names_arg",
]
