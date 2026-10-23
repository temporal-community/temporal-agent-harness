# @temporalio/agent-harness-svelte

A Svelte 5 binding for `@temporalio/agent-harness-client`: `AgentSession`, a reactive handle on one
harness agent session.

```svelte
<script lang="ts">
  import { AgentSession } from "@temporalio/agent-harness-svelte";
  import type { TicTacToeAgent } from "./client_sdk/TicTacToeAgent"; // from harness-codegen

  let { sessionId } = $props();
  const agent = new AgentSession<TicTacToeAgent>({ get sessionId() { return sessionId; } });
  const board = $derived(agent.states.board);
</script>

{#if board}
  {#each board.cells as cell, i (i)}
    <button onclick={() => agent.sendMessage("play", { cell: i + 1 })}>{cell ?? ""}</button>
  {/each}
{/if}
```

- **Reading opens the connection.** The first effect or markup to read any field opens the
  session's stream; it closes once nothing reads it, so unmounting the last view releases it.
- **Getter options follow changes.** A `get sessionId()` switches the session when its value does.
- **One connection per session.** Call `createHarnessContext()` in a parent component and every
  `AgentSession` below it with the same session id shares one connection and one state.
- **The whole tree.** `messages`, `states` and `agentStatus` are the root agent's; `agents` and
  `agent(id)` reach every subagent, and a `subagent` part's `messageId` names the child message
  it ran. `pendingApprovals` and `pendingCallbacks` collect waiting tool calls from all of them.
- **Key message rows on `message.key`** (`{#each messages as m (m.key)}`), not `m.id`. A send's
  `id` changes from its local id to the server's when it is admitted; its `key` does not, so the
  row keeps its DOM, focus and measured height. (From the client release after 0.1.0.)

## Rendering replies as Markdown

A reply is model output, so treat its Markdown as untrusted. With `@tanstack/markdown`, which
escapes raw HTML by default, use `urlTransform` for two more rules:
- keep links only to `http:`, `https:` and `mailto:`;
- render images as their alt text. The renderer loads remote images by default, so a reply could
  plant a tracking pixel or an image URL that carries data out.

```svelte
<script lang="ts">
  import type { UrlTransform } from "@tanstack/markdown";
  import { renderHtml } from "@tanstack/markdown/html";
  import { parseMarkdown } from "@tanstack/markdown/parser";

  let { text }: { text: string } = $props();

  const urlTransform: UrlTransform = (url, kind, defaultUrl) =>
    kind === "link" && defaultUrl && /^(https?:|mailto:)/i.test(defaultUrl) ? defaultUrl : null;
  // Once per change of `text`, which the session batches per flush.
  const html = $derived(renderHtml(parseMarkdown(text, { urlTransform })));
</script>

<div class="reply">{@html html}</div>
```

`null` from `urlTransform` drops the link or image but keeps its text. Relative links are
dropped too, since a reply has no base to be relative to.

To show some images, allow only sources that cannot phone home, as the console does: a raster
data URL (`/^data:image\/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/]*={0,2}$/i`, never SVG, which
can carry script), capped in size, or a content-addressed path your own API serves. Return `url`
for `kind === "image"` when it passes, and keep `null` for everything else.

## Rendering replies per handler

With generated types, `message.handler` narrows `message.output` to that handler's reply, so a
map from handler to component can be typed end to end. Handlers without a card fall back to
JSON:

```svelte
<!-- Reply.svelte -->
<script lang="ts">
  import type { Component } from "svelte";
  import {
    parsePartialJson,
    type AgentSchema,
    type HandlerName,
    type HandlerOutput,
    type HarnessMessage
  } from "@temporalio/agent-harness-client";
  import type { TicTacToeAgent } from "./client_sdk/TicTacToeAgent"; // from harness-codegen
  import MoveCard from "./MoveCard.svelte"; // props: { output: Partial<TextReply> }

  type Cards<A extends AgentSchema> = {
    [H in HandlerName<A>]?: Component<{ output: Partial<HandlerOutput<A, H>> }>;
  };
  const cards: Cards<TicTacToeAgent> = { play: MoveCard };

  let { message }: { message: HarnessMessage<TicTacToeAgent> } = $props();

  // TypeScript can't tie the lookup to the narrowed message, so it is cast once here.
  const Card = $derived(cards[message.handler] as Component<{ output: unknown }> | undefined);
  // The reply text streamed so far: a data handler streams its output as JSON.
  const text = $derived(
    message.output == null && Card ? message.parts.map((part) => (part.type === "reply_delta" ? part.text : "")).join("") : ""
  );
  // Once per flush; strip a ```json fence a model may open with.
  const draft = $derived(text ? parsePartialJson(text.replace(/^\s*```(?:json)?\s*/, "")) : undefined);
  const output = $derived(message.output ?? draft);
</script>

{#if output != null}
  {#if Card}<Card {output} />{:else}<pre>{JSON.stringify(output, null, 2)}</pre>{/if}
{/if}
```

While the reply streams, `parsePartialJson` reads the JSON so far as a document: it closes open
strings and brackets and drops a half-written key or value. A card therefore gets a `Partial`
of its output and should draw a missing field as a placeholder. The parser is best effort, and
quadratic on text that never parses, so run it once per flush and only for handlers with a
card. (`parsePartialJson` is in the client release after 0.1.0.)

## Forms from a handler's schema

`GET /api/agent-interface/{sessionId}` lists each handler with `parameters`, the JSON Schema
of its input. The client turns that schema into fields (`describeSchema`), checks values
against it (`validate`, keyed by field path such as `address.city`) and builds the payload
(`buildPayload`). The server validates again and is the authority. (These helpers are in the
client release after 0.1.0.)

With native inputs, the schema's constraints become attributes the browser checks too:

```svelte
<!-- SchemaInput.svelte -->
<script lang="ts">
  import type { SchemaField } from "@temporalio/agent-harness-client";

  let { field, value, onchange }: { field: SchemaField; value: unknown; onchange: (value: unknown) => void } = $props();
  const required = $derived(field.required && field.kind !== "boolean");
</script>

{#if field.kind === "boolean"}
  <input id={field.name} type="checkbox" checked={value === true} onchange={(e) => onchange(e.currentTarget.checked)} />
{:else if field.kind === "enum"}
  <select id={field.name} {required} value={String(value)} onchange={(e) => onchange(e.currentTarget.value)}>
    {#each field.choices ?? [] as choice (choice)}<option>{choice}</option>{/each}
  </select>
{:else if field.kind === "integer" || field.kind === "number"}
  <input id={field.name} {required} type="number" step={field.kind === "integer" ? 1 : "any"} min={field.minimum}
    max={field.maximum} value={String(value)} oninput={(e) => onchange(e.currentTarget.value)} />
{:else}
  <!-- Text. `pattern` is anchored by the browser but not by JSON Schema, hence the `.*`s. Give
       "json", "array" and "object" fields a textarea or recurse into `field.fields`/`field.item`. -->
  <input id={field.name} {required} minlength={field.minLength} maxlength={field.maxLength}
    pattern={field.pattern && `.*(?:${field.pattern}).*`} value={String(value)} oninput={(e) => onchange(e.currentTarget.value)} />
{/if}
```

```svelte
<!-- HandlerForm.svelte -->
<script lang="ts">
  import { buildPayload, describeSchema, emptyValues, validate } from "@temporalio/agent-harness-client";
  import { AgentSession } from "@temporalio/agent-harness-svelte";
  import SchemaInput from "./SchemaInput.svelte";

  /** `handler` is one entry of `GET /api/agent-interface/{sessionId}`. */
  let { sessionId, handler }: { sessionId: string; handler: { name: string; parameters: Record<string, unknown> } } = $props();
  const agent = new AgentSession({ get sessionId() { return sessionId; } });
  const fields = $derived(describeSchema(handler.parameters));
  // Reassigned on each edit (Svelte 5.25 or later), and blank again when `handler` changes.
  let values = $derived(emptyValues(fields));
  let problems = $state<Record<string, string>>({});

  function submit(event: SubmitEvent) {
    event.preventDefault();
    problems = validate(fields, values);
    if (Object.keys(problems).length === 0) void agent.sendMessage(handler.name, buildPayload(fields, values));
  }
</script>

<form onsubmit={submit}>
  {#each fields as field (field.name)}
    <p>
      <label for={field.name}>{field.title}</label>
      <SchemaInput {field} value={values[field.name]} onchange={(value) => (values = { ...values, [field.name]: value })} />
      {#if problems[field.name]}<span role="alert">{problems[field.name]}</span>{/if}
    </p>
  {/each}
  <button>Send</button>
</form>
```

With [TanStack Form](https://tanstack.com/form), wrap `validate` as a
[Standard Schema](https://standardschema.dev), which TanStack Form accepts as a validator and
whose issue paths it maps onto fields. The same `SchemaInput` renders each field:

```svelte
<!-- HandlerTanStackForm.svelte: wrap it in {#key handler.name} so another handler starts blank. -->
<script lang="ts">
  import { createForm, type StandardSchemaV1 } from "@tanstack/svelte-form";
  import { buildPayload, describeSchema, emptyValues, validate, type SchemaField } from "@temporalio/agent-harness-client";
  import { AgentSession } from "@temporalio/agent-harness-svelte";
  import SchemaInput from "./SchemaInput.svelte";

  /** The client's `validate` as a Standard Schema: each problem is an issue at its field's path. */
  const standardSchema = (fields: SchemaField[]): StandardSchemaV1<Record<string, unknown>> => ({
    "~standard": {
      version: 1,
      vendor: "agent-harness",
      validate: (input) => {
        const value = input as Record<string, unknown>;
        const problems = Object.entries(validate(fields, value));
        return problems.length === 0
          ? { value }
          : { issues: problems.map(([path, message]) => ({ message, path: path.split(".") })) };
      }
    }
  });

  let { sessionId, handler }: { sessionId: string; handler: { name: string; parameters: Record<string, unknown> } } = $props();
  const agent = new AgentSession({ get sessionId() { return sessionId; } });
  const fields = $derived(describeSchema(handler.parameters));
  const form = createForm(() => ({
    defaultValues: emptyValues(fields),
    validators: { onSubmit: standardSchema(fields) },
    onSubmit: ({ value }) => agent.sendMessage(handler.name, buildPayload(fields, value))
  }));
</script>

<form onsubmit={(event) => { event.preventDefault(); void form.handleSubmit(); }}>
  {#each fields as schemaField (schemaField.name)}
    <form.Field name={schemaField.name}>
      {#snippet children(field)}
        <p>
          <label for={schemaField.name}>{schemaField.title}</label>
          <SchemaInput field={schemaField} value={field.state.value} onchange={field.handleChange} />
          {#each field.state.meta.errors as issue (issue?.message)}<span role="alert">{issue?.message}</span>{/each}
        </p>
      {/snippet}
    </form.Field>
  {/each}
  <button>Send</button>
</form>
```

## A virtual transcript

For long sessions, render only the visible messages with `@tanstack/svelte-virtual`:
- `anchorTo: "end"` keeps the view on the latest message as replies grow.
- `followOnAppend` follows new messages only while the reader is already at the end.
- `getItemKey` uses `message.key`, so a send keeps its measured height when the server admits it.

```svelte
<script lang="ts">
  import { createVirtualizer } from "@tanstack/svelte-virtual";
  import { get } from "svelte/store";

  let { sessionId }: { sessionId: string } = $props();
  const agent = new AgentSession({ get sessionId() { return sessionId; } });
  const messages = $derived([...agent.messages, ...agent.pending]);

  let scroller = $state<HTMLDivElement | null>(null);
  const virtualizer = createVirtualizer<HTMLDivElement, HTMLDivElement>({
    count: 0,
    getScrollElement: () => scroller,
    estimateSize: () => 72,
    anchorTo: "end",
    followOnAppend: true,
    overscan: 6
  });
  $effect(() => {
    const list = messages;
    // `get`, not `$virtualizer`: setOptions updates the store, which would re-run this effect.
    get(virtualizer).setOptions({ count: list.length, getItemKey: (index) => list[index]!.key });
  });
  const measure = (row: HTMLDivElement) => get(virtualizer).measureElement(row);
</script>

<div bind:this={scroller} tabindex="-1" style="height: 600px; overflow: auto">
  <div style="position: relative; height: {$virtualizer.getTotalSize()}px">
    {#each $virtualizer.getVirtualItems() as item (item.key)}
      <div {@attach measure} data-index={item.index} style="position: absolute; width: 100%; transform: translateY({item.start}px)">
        <Message message={messages[item.index]} />
      </div>
    {/each}
  </div>
</div>
```

A row scrolled out of the window unmounts, taking focus with it. When that happens, move focus to
the row now at the same index, or to the list itself (hence its `tabindex="-1"`).

## With TanStack Query

The session list and session creation are plain request/response calls, so they suit TanStack
Query. Sends, approvals, callbacks and status stay in the session: they go through
`AgentSession` and arrive on its stream, not through queries or mutations.

```svelte
<script lang="ts">
  import { createMutation, createQuery, queryOptions, useQueryClient } from "@tanstack/svelte-query";
  import { HttpTransport, startSession } from "@temporalio/agent-harness-svelte";
  import { TicTacToeAgent } from "./client_sdk/TicTacToeAgent"; // from harness-codegen

  const transport = new HttpTransport({ baseUrl: "/api/" });
  const sessionsQuery = () =>
    queryOptions({
      queryKey: ["agent-harness", "sessions"],
      queryFn: ({ signal }) => transport.listSessions(signal)
    });

  const queryClient = useQueryClient();
  const sessions = createQuery(sessionsQuery);
  const start = createMutation(() => ({
    mutationFn: () => startSession(transport, TicTacToeAgent),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: sessionsQuery().queryKey })
  }));
</script>

<button onclick={() => start.mutate()} disabled={start.isPending}>New session</button>
{#each sessions.data ?? [] as s (s.workflow_id)}
  <a href="?session={s.workflow_id}">{s.label}</a>
{/each}
```

## Development

This package is part of the pnpm workspace in `typescript/`, and builds and tests against the
`@temporalio/agent-harness-client` in the working tree; its published dependency is the
`^<version>` range in `package.json`. `bazel test //typescript/packages/svelte/...` builds and
tests it. Without Bazel, build the client first, then run these commands from
`typescript/packages/svelte` (see
[Testing local changes](../README.md#testing-local-changes-without-publishing)):

```sh
pnpm install
pnpm run check && pnpm test && pnpm run build
```
