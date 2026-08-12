import type { Component } from "svelte";
import type {
  AgentWorkspaceProps,
  ToolPresentation
} from "$lib/components/agent/AgentChatPanel.svelte";
import type { AgentPresentationAdapter } from "$lib/state/messagePresentation";

export interface AgentUiExtensions {
  headerControl?: Component<Record<string, never>>;
  workspaceComponent?: Component<AgentWorkspaceProps>;
  toolPresentation?: ToolPresentation;
  presentation?: AgentPresentationAdapter;
}
