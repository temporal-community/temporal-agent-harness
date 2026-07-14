"""Agent and command models."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from annotated_types import Predicate
from pydantic import Field

from .base import OpenCodeBaseModel
from .common import ModelRef  # noqa: TC001


ThemeColor = Literal["primary", "secondary", "accent", "success", "warning", "error", "info"]
"""Predefined theme color names."""

HexColor = Annotated[
    str,
    Predicate(
        lambda s: len(s) == 7 and s[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in s[1:])  # noqa: PLR2004
    ),
]
"""Hex color code in #RRGGBB format."""

AgentColor = HexColor | ThemeColor
"""Agent color: hex code (#FF5733) or theme color name."""
PermissionBehavior = Literal["ask", "allow", "deny"]
AgentMode = Literal["subagent", "primary", "all"]


class AgentPermission(OpenCodeBaseModel):
    """Agent permission settings."""

    edit: PermissionBehavior = "ask"
    bash: dict[str, PermissionBehavior] = Field(default_factory=dict)
    skill: dict[str, PermissionBehavior] = Field(default_factory=dict)
    webfetch: PermissionBehavior | None = None
    doom_loop: PermissionBehavior | None = None
    external_directory: PermissionBehavior | None = None


class Agent(OpenCodeBaseModel):
    """Agent information matching SDK type."""

    name: str
    description: str | None = None
    mode: AgentMode = "primary"
    native: bool | None = None
    hidden: bool | None = None
    default: bool | None = None
    top_p: float | None = None
    temperature: float | None = None
    color: AgentColor | None = None
    permission: AgentPermission = Field(default_factory=AgentPermission)
    model: ModelRef | None = None
    prompt: str | None = None
    tools: dict[str, bool] = Field(default_factory=dict)
    options: dict[str, str] = Field(default_factory=dict)


class Command(OpenCodeBaseModel):
    """Slash command."""

    name: str
    description: str = ""


class SkillInfo(OpenCodeBaseModel):
    """Skill information."""

    name: str
    """Skill name."""

    description: str
    """Skill description."""

    location: str
    """File path where the skill is defined."""

    content: str
    """Skill content (e.g. SKILL.md body)."""


class ProviderAuthMethod(OpenCodeBaseModel):
    """Authentication method for a provider."""

    type: Literal["oauth", "api"]
    """Auth type."""

    label: str
    """Human-readable label for the auth method."""


class ProviderAuthAuthorization(OpenCodeBaseModel):
    """Response from starting a provider OAuth flow."""

    url: str
    """URL to open in browser for authorization."""

    method: Literal["auto", "code"]
    """Authorization method."""

    instructions: str
    """Instructions to display to the user."""


class WorktreeInfo(OpenCodeBaseModel):
    """Git worktree information."""

    name: str
    """Worktree name."""

    branch: str
    """Git branch name."""

    directory: str
    """Full path to the worktree directory."""


class WorktreeCreateRequest(OpenCodeBaseModel):
    """Request to create a new git worktree."""

    name: str | None = None
    """Optional worktree name. Auto-generated if not provided."""

    start_command: str | None = None
    """Optional startup script to run after creation."""


class WorktreeRemoveRequest(OpenCodeBaseModel):
    """Request to remove a git worktree."""

    directory: str
    """Worktree directory path to remove."""


class WorktreeResetRequest(OpenCodeBaseModel):
    """Request to reset a git worktree."""

    directory: str
    """Worktree directory path to reset."""


class OAuthAuthInfo(OpenCodeBaseModel):
    """OAuth authentication credentials."""

    type: Literal["oauth"]
    """Auth type discriminator."""

    refresh: str
    """Refresh token."""

    access: str
    """Access token."""

    expires: int
    """Token expiry timestamp."""

    account_id: str | None = None
    """Optional account identifier."""

    enterprise_url: str | None = None
    """Optional enterprise URL."""


class ApiAuthInfo(OpenCodeBaseModel):
    """API key authentication credentials."""

    type: Literal["api"]
    """Auth type discriminator."""

    key: str
    """API key."""


class WellKnownAuthInfo(OpenCodeBaseModel):
    """Well-known authentication credentials."""

    type: Literal["wellknown"]
    """Auth type discriminator."""

    key: str
    """Key identifier."""

    token: str
    """Authentication token."""


AuthInfo = OAuthAuthInfo | ApiAuthInfo | WellKnownAuthInfo
"""Authentication credentials (discriminated union on 'type')."""


class WorkspaceInfo(OpenCodeBaseModel):
    """Workspace information matching OpenCode SDK type."""

    id: str
    """Workspace identifier."""

    type: Literal["worktree"] | str  # noqa: PYI051
    """Workspace type."""

    branch: str | None = None
    """Git branch associated with the workspace."""

    name: str | None = None
    """Workspace display name."""

    directory: str | None = None
    """Directory path of the workspace."""

    extra: Any | None = None
    """Additional workspace-specific data."""

    project_id: str
    """ID of the project this workspace belongs to."""


class WorkspaceCreateRequest(OpenCodeBaseModel):
    """Request to create a workspace."""

    type: Literal["worktree"] | str  # noqa: PYI051
    """Workspace type."""

    branch: str | None = None
    """Git branch for the workspace."""

    extra: Any | None = None
    """Additional workspace-specific data."""
