// Compile-time checks that the wire values core declares by hand (as const objects, so code can
// name them) are exactly the generated protocol's: `tsc` fails if either side changes alone.

import type * as Protocol from "@temporalio/agent-harness-client";
import type { AutoApprovalVerdict, MessageDisposition } from "../src/harness/agent-protocol/index.ts";

type Equals<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
const holds = <T extends true>(): T => true as T;

holds<Equals<MessageDisposition, Protocol.MessageDisposition>>();
holds<Equals<AutoApprovalVerdict, Protocol.AutoApprovalVerdict>>();
