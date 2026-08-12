import type {
  ChroniclerAudioDestinationApproval,
  ChroniclerAudioSnapshot
} from "$lib/bridge/api";

export interface AudioGenerationPresentation {
  snapshot: ChroniclerAudioSnapshot;
  generationId: string;
  toolId?: string;
  cancellation: { enabled: boolean; detail: string };
  destinationApproval: ChroniclerAudioDestinationApproval | null;
  destinationAuthority: { ready: boolean; detail: string };
  recoveryAvailable?: boolean;
  onApproveDestination?: (approval: ChroniclerAudioDestinationApproval) => void | Promise<void>;
  onCancel?: (childWorkflowId: string) => void | Promise<void>;
  onRecover?: (snapshot: ChroniclerAudioSnapshot) => void | Promise<void>;
}
