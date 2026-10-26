// The Temporal worker plugin for the Gemini integration (Python's _google_genai_plugin.py).

import { SimplePlugin } from "@temporalio/plugin";
import { GEMINI_INTERACTIONS_CREATE_STREAMED_ACTIVITY } from "@temporalio/agent-harness/ai-sdks/google-genai-plugin";
import {
  makeGeminiInteractionsCreateStreamed,
  type GeminiInteractionsClient,
} from "./interactions-activity.ts";

/**
 * Registers the Gemini activities that the workflow-side `googleGenaiClient` calls, run against
 * `client`, a fully configured `GoogleGenAI` (its credentials, HTTP options and the rest are the
 * caller's). No credentials pass to or from the workflow. Experimental.
 *
 *   const plugin = new GoogleGenAIPlugin(new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY }));
 *
 * Registration is first-one-wins: an activity of the same name the worker registers itself is
 * kept.
 */
export class GoogleGenAIPlugin extends SimplePlugin {
  constructor(client: GeminiInteractionsClient) {
    const activities = {
      [GEMINI_INTERACTIONS_CREATE_STREAMED_ACTIVITY]: makeGeminiInteractionsCreateStreamed(client),
    };
    super({
      name: "GoogleGenAIPlugin",
      activities: (existing) => ({ ...activities, ...existing }),
    });
  }
}
