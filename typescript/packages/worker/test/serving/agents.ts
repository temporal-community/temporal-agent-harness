// A TypeScript agent the Python server serves (python/tests/web/test_typescript_agents.py). It
// takes required init data, so its start form comes from the schema document agent-harness-schema
// writes, and its reply carries both that data and the message, so a test sees each arrive.

import { z } from "zod";
import {
  TextMessage,
  TextReply,
  ToolApprovalPolicy,
  defineAgent,
  handler,
} from "@temporalio/agent-harness";

export const BriefedTsAgent = defineAgent({
  name: "BriefedTsAgent",
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  initData: {
    schema: z
      .object({ topic: z.string().describe("What the agent works on.") })
      .meta({ title: "Brief", description: "What this session is about." }),
    required: true,
  },
  handlers: {
    ask: handler({ description: "Ask about the topic.", input: TextMessage, output: TextReply }),
  },
  create: ({ data }) => ({ ask: async ({ text }) => ({ text: `${data.topic}: ${text}` }) }),
});
