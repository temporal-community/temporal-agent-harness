// jev_tool_approval: ask TypeSafe's Jev to judge one gated tool call

import { Context, ApplicationFailure } from "@temporalio/activity";
import type { JevApprovalAnswer, JevApprovalRequest } from "@temporalio/agent-harness/ai-sdks/jev/evaluator";

export interface SystemOneResponse {
  model: string;
  request_id?: string;
  usage?: { input_tokens?: number | null; output_tokens?: number | null };
  answers: Record<
    string,
    {
      type: string;
      choice?: string;
      confidence?: number;
      probabilities?: Record<string, number>;
      noul?: number;
    }
  >;
}

const JEV_REJECTED_STATUS = new Set([400, 401, 403, 404, 422]);

export interface SystemOneEndpoint {
  base: string;
  apiKey: string;
  /** The wire model for a requested one; undefined lets the gateway pick its default. */
  model(requested: string): string | undefined;
}

/**
 * Where System One requests go, by whichever key is set: TypeSafe directly, else OpenRouter, else
 * Vercel AI Gateway. Both gateways serve the TypeSafe wire format at `{base}/v1/systemone`; only
 * the `jev-latest` alias is renamed for them.
 */
export function systemOneEndpoint(env: NodeJS.ProcessEnv = process.env): SystemOneEndpoint | null {
  const typesafe = env.TYPESAFE_API_KEY ?? env.TYPESAFE_AI_API_KEY;
  if (typesafe)
    return {
      base: env.TYPESAFE_BASE_URL ?? "https://api.typesafe.ai",
      apiKey: typesafe,
      model: (m) => m,
    };
  if (env.OPENROUTER_API_KEY) {
    return {
      base: "https://openrouter.ai/api",
      apiKey: env.OPENROUTER_API_KEY,
      model: (m) => (m === "jev-latest" ? "~typesafe/jev-latest" : m),
    };
  }
  if (env.AI_GATEWAY_API_KEY) {
    return {
      base: "https://ai-gateway.vercel.sh/typesafe",
      apiKey: env.AI_GATEWAY_API_KEY,
      model: (m) => (m === "jev-latest" ? undefined : m),
    };
  }
  return null;
}

/**
 * POST one System One request from an activity. There is no TypeScript TypeSafe SDK, so this calls
 * the same endpoint the Python SDK does (POST {base}/v1/systemone with a bearer key), on TypeSafe
 * or a gateway (see `systemOneEndpoint`). A missing key (`JevNotConfigured`) and HTTP
 * 400/401/403/404/422 (`JevRejected`) fail non-retryably; other failures retry under the caller's policy.
 * Outside an activity (a CLI, an eval), pass the `signal` to abort on.
 */
export async function postSystemOne(
  request: JevApprovalRequest,
  signal?: AbortSignal,
): Promise<Response> {
  const endpoint = systemOneEndpoint();
  if (endpoint === null) {
    throw ApplicationFailure.create({
      type: "JevNotConfigured",
      nonRetryable: true,
      message:
        "none of TYPESAFE_API_KEY, OPENROUTER_API_KEY or AI_GATEWAY_API_KEY is set on this worker",
    });
  }
  const response = await fetch(`${endpoint.base}/v1/systemone`, {
    method: "POST",
    headers: { Authorization: `Bearer ${endpoint.apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      state: request.state,
      questions: request.questions,
      model: endpoint.model(request.model ?? process.env.TYPESAFE_DEFAULT_MODEL ?? "jev-latest"),
    }),
    signal: signal ?? Context.current().cancellationSignal,
  });
  if (response.ok) return response;
  const message = `TypeSafe system_one failed: ${response.status} ${await response.text()}`;
  if (JEV_REJECTED_STATUS.has(response.status))
    throw ApplicationFailure.create({ type: "JevRejected", nonRetryable: true, message });
  throw new Error(message);
}

export async function jevToolApproval(request: JevApprovalRequest): Promise<JevApprovalAnswer> {
  const response = await postSystemOne(request);
  const body = (await response.json()) as SystemOneResponse;
  const verdict = body.answers.verdict;
  return {
    model: body.model,
    request_id: response.headers.get("x-request-id") ?? body.request_id ?? "",
    verdict: verdict?.choice ?? "",
    verdict_confidence: verdict?.confidence ?? 0,
    verdict_probabilities: verdict?.probabilities ?? {},
    irreversible: body.answers.irreversible?.noul ?? 1,
    input_tokens: body.usage?.input_tokens ?? null,
    output_tokens: body.usage?.output_tokens ?? null,
  };
}
