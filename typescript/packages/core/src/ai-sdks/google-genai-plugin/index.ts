// The Google Gemini integration's workflow side (Python's google_genai_plugin). Experimental.
//
// `googleGenaiClient` gives workflow code a Gemini client whose network calls run as activities on
// the worker's real client, registered by `GoogleGenAIPlugin` from
// `@temporalio/agent-harness-worker/ai-sdks/google-genai-plugin`; no credentials enter the
// workflow or its history. The Interactions API has no automatic function calling, so the workflow
// drives the tool loop: declare tools with `functionParam`, keep the conversation in an
// `InteractionConversation`, and run each call with `runner.runTool`.
//
//   const gemini = googleGenaiClient({ runner });
//   conversation.addUserText(text);
//   while (true) {
//     const stream = await gemini.interactions.create({
//       model, input: conversation.steps, tools: tools.map(functionParam), stream: true,
//     });
//     const reply = await conversation.readReply(stream);
//     if (reply.functionCalls.length === 0) return reply.text;
//     conversation.addFunctionResults(await Promise.all(reply.functionCalls.map(runCall)));
//   }

export {
  InteractionConversation,
  type FunctionCallStep,
  type FunctionResultStepParam,
  type InteractionReply,
} from "./interactions-conversation.ts";
export type { InteractionResult } from "./interactions-models.ts";
export { functionParam } from "./interactions-workflow.ts";
export { TemporalAsyncClient } from "./temporal-async-client.ts";
export {
  GEMINI_INTERACTIONS_CREATE_STREAMED_ACTIVITY,
  TemporalAsyncInteractionStream,
  TemporalAsyncInteractions,
  type GeminiInteractionsActivity,
  type InteractionCreateParamsStreaming,
} from "./temporal-interactions.ts";
export { googleGenaiClient } from "./workflow.ts";
