// A probe agent whose Code Mode scripts work on an activity-backed mount, for activity-fs.test.ts.
// The worker registers the filesystem's activities over a directory of its choosing.

import { z } from "zod";
import {
  Mount,
  TextMessage,
  TextReply,
  ToolApprovalPolicy,
  activityFilesystem,
  codeModeTool,
  defineAgent,
  handler,
} from "@temporalio/agent-harness";

export const diskFilesystem = activityFilesystem({
  name: "test-disk",
  configSchema: z.object({}),
  writable: true,
});

export const DiskProbeAgent = defineAgent({
  name: "DiskProbeAgent",
  approvalPolicyDefault: ToolApprovalPolicy.allowTools(
    ["run_code", "fs_stat", "fs_read", "fs_list", "fs_write", "fs_mkdir", "fs_delete", "fs_rename"],
    { alsoInherentlySafe: true },
  ),
  handlers: {
    run: handler({ description: "Run a Code Mode script.", input: TextMessage, output: TextReply }),
  },
  create: ({ runner }) => {
    const codeTool = codeModeTool([], {
      name: "run_code",
      mounts: [new Mount("/disk", diskFilesystem.backend({}), { description: "A directory." })],
    });
    return {
      run: async ({ text }) => ({
        text: await runner.runTool("code-1", codeTool, { script: text }),
      }),
    };
  },
});
