// A Code Mode tool for one OKF bundle: the mount, three host functions, and a contract section
// that explains the format. Reads and writes stay ordinary file operations, so they are indexed.

import { workflowInfo } from "@temporalio/workflow";
import { z } from "zod";
import { injected, tool } from "../agent.ts";
import { ActivityBackend } from "./activity-fs.ts";
import { codeModeTool, type CodeModeTool } from "./tool.ts";
import { OKFConcept, OKFLinks, renderConcept } from "./okf.ts";
import type { Mount } from "./vfs.ts";

const BUNDLE = "okf_bundle";

interface Bundle {
  backend: ActivityBackend;
  mount: string;
}

const bundleInjection = { [BUNDLE]: injected<Bundle>() };

const okfConcepts = tool({
  name: "okf_concepts",
  description:
    'Every concept in the OKF bundle, from its frontmatter, without reading bodies. `prefix` limits it to the concepts under a folder, by concept id (e.g. "trips"). Each concept\'s `path` is the file to read; `problem` says what is wrong with a concept whose frontmatter is missing or broken.',
  inputSchema: z.object({ prefix: z.string().default("") }),
  outputSchema: z.array(OKFConcept),
  inherentlySafe: true,
  injected: bundleInjection,
  execute: async ({ prefix }, { okf_bundle }) => {
    const stripped = prefix.replace(/^\/+|\/+$/g, "");
    const graph = await okf_bundle.backend.okfGraph(stripped === "" ? "." : stripped, false);
    return graph.concepts.map((concept) => ({
      ...concept,
      path: `${okf_bundle.mount}/${concept.path}`,
    }));
  },
});

const okfLinks = tool({
  name: "okf_links",
  description:
    "The concepts the concept `id` links to, and the concepts that link to it (by concept id). `dangling` marks a link to a concept that doesn't exist yet.",
  inputSchema: z.object({ id: z.string() }),
  outputSchema: OKFLinks,
  inherentlySafe: true,
  injected: bundleInjection,
  execute: async ({ id }, { okf_bundle }) => {
    const graph = await okf_bundle.backend.okfGraph(".", true);
    const conceptId = id.replace(/^\/+|\/+$/g, "").replace(/\.md$/, "");
    return {
      id: conceptId,
      links_to: graph.links.filter((link) => link.source === conceptId),
      cited_by: graph.links.filter((link) => link.target === conceptId),
    };
  },
});

const okfRender = tool({
  name: "okf_render",
  description:
    "A concept's full text: `frontmatter` as YAML between `---` lines, then the markdown `body`. Fills in `generated` (who wrote it, and when) unless you give one, and the time of a `verified` entry given without `at`. Write the result to the concept's file with `Path(...).write_text(...)`.",
  inputSchema: z.object({
    frontmatter: z.record(z.string(), z.any()),
    body: z.string(),
  }),
  outputSchema: z.string(),
  inherentlySafe: true,
  execute: async ({ frontmatter, body }) =>
    renderConcept(frontmatter, body, {
      by: workflowInfo().workflowType,
      at: new Date(),
    }),
});

function guide(mount: string, version: string): string {
  return `
Knowledge bundle: \`${mount}\` is an Open Knowledge Format (OKF v${version}) bundle, a folder of markdown files you read and write with \`open()\` and \`pathlib\` like any other.

- Every \`.md\` file is a concept, except \`index.md\` and \`log.md\`. A concept's id is its path in the bundle without \`.md\` (\`${mount}/trips/denver.md\` is \`trips/denver\`).
- A concept starts with YAML frontmatter between \`---\` lines, then a markdown body. \`type\` is required: a short descriptive kind, such as \`Person\` or \`Plan\`. Also give \`title\` and \`description\` (ONE sentence). Optional: \`tags\` (a list), \`status\` (\`draft\`, \`stable\` or \`deprecated\`), \`stale_after\` (a UTC timestamp after which the concept is out of date), \`verified\` (a list of \`{by}\`, with \`by\` as \`human:<id>\` when a person confirmed it; \`okf_render\` adds the time), and \`sources\` (a list of \`{id, resource, title}\`).
- Write concepts with \`okf_render(frontmatter, body)\`, which formats the frontmatter and records who wrote the concept and when: \`Path(...).write_text(await okf_render({...}, body))\`.
- Link related concepts with ordinary markdown links relative to the linking file, e.g. \`[Ana](../people/ana.md)\`. Link only to concepts that exist, never to the concept itself, and once per mention.
- Each folder has an \`index.md\` listing its concepts, one line each: \`* [Title](slug.md) - description\`, and subfolders as \`* [folder](folder/index.md) - summary\`. Keep it up to date whenever you add, retitle or remove a concept.
- \`${mount}/log.md\` records changes, newest first, under \`## YYYY-MM-DD\` headings: \`* **Creation**: [Title](path.md) ...\`. Add an entry for every change.
- To find concepts, call \`okf_concepts()\` rather than reading files one by one; then read the files you need. \`okf_links(id)\` gives a concept's links and backlinks.
- Treat concepts marked \`stale\` or \`deprecated\` as out of date, and say so when you use them.
`;
}

export function okfCodeModeTool(
  bundle: Mount,
  options: {
    name: string;
    tools?: Parameters<typeof codeModeTool>[0];
    mounts?: readonly Mount[];
    injections?: Record<string, unknown>;
    inherentlySafe?: boolean;
    autoApprovalCriteria?: string;
  },
): CodeModeTool {
  const backend = bundle.backend;
  if (!(backend instanceof ActivityBackend) || !backend.okfVersion || bundle.tracked === null)
    throw new TypeError(
      `okfCodeModeTool takes a bound okfBundleVfsMount(...), got '${bundle.path}'`,
    );
  if (options.injections && Object.hasOwn(options.injections, BUNDLE))
    throw new TypeError(`injection name '${BUNDLE}' is reserved by okfCodeModeTool`);
  return codeModeTool([...(options.tools ?? []), okfConcepts, okfLinks, okfRender], {
    name: options.name,
    inherentlySafe: options.inherentlySafe,
    ...(options.autoApprovalCriteria ? { autoApprovalCriteria: options.autoApprovalCriteria } : {}),
    injections: {
      ...options.injections,
      [BUNDLE]: { backend, mount: bundle.path } satisfies Bundle,
    },
    mounts: [...(options.mounts ?? []), bundle],
    guide: guide(bundle.path, backend.okfVersion),
  });
}
