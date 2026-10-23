# @temporalio/agent-harness-react

A React binding for `@temporalio/agent-harness-client`: `useAgentSession`, a hook that follows one
harness agent session. React 18.2 or later.

```tsx
import { useAgentSession } from "@temporalio/agent-harness-react";
import type { TicTacToeAgent } from "./client_sdk/TicTacToeAgent"; // from harness-codegen

function Board({ sessionId }: { sessionId: string }) {
  const agent = useAgentSession<TicTacToeAgent>({ sessionId });
  const board = agent.states.board;
  if (!board) return null;
  return board.cells.map((cell, i) => (
    <button key={i} onClick={() => agent.sendMessage("play", { cell: i + 1 })}>{cell ?? ""}</button>
  ));
}
```

- **Mounting opens the connection.** The first mounted component using a session opens its
  stream; it closes once the last one unmounts. StrictMode's extra unmount and remount in
  development does not reconnect.
- **Renders follow the stream.** The hook reads the session through `useSyncExternalStore`, and
  the core writes once per flush, so a burst of frames is one render.
- **Select to render less.** `useAgentSessionSelector(options, (s) => s.connection)` re-renders
  only when the selected value changes (by `Object.is`), so a status badge sits out every flush
  that only adds frames or messages. It reads the raw `SessionSnapshot`, not the `AgentSession`
  view.
- **A changed `sessionId` moves the connection.** Pass it as a prop or state like any other value.
- **One connection per session.** Put a `<HarnessProvider>` above your components and every
  `useAgentSession` below it with the same session id shares one connection and one state.
  Without one, each component gets its own.
- **The first component to open a session decides its options** (`callbackTools`, transport and
  so on). Later components, and later renders, share what it set up, so passing a new
  `callbackTools` object each render is fine but does not replace the handlers.
- **The whole tree.** `messages`, `states` and `agentStatus` are the root agent's; `agents` and
  `agent(id)` reach every subagent, and a `subagent` part's `messageId` names the child message
  it ran. `pendingApprovals` and `pendingCallbacks` collect waiting tool calls from all of them.
- **Key message rows on `message.key`**, not `message.id`. A send's `id` changes from its local
  id to the server's when it is admitted; its `key` does not, so the row keeps its DOM, focus
  and measured height. (From the client release after 0.1.0.)
- **Server rendering** shows the empty session and opens no connection; the client connects once
  it hydrates.

## With TanStack Query

The session list and session creation are plain request/response calls, so they suit TanStack
Query. Sends, approvals, callbacks and status stay in the session: they go through
`useAgentSession` and arrive on its stream, not through queries or mutations.

```tsx
import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { HttpTransport, startSession } from "@temporalio/agent-harness-react";
import { TicTacToeAgent } from "./client_sdk/TicTacToeAgent"; // from harness-codegen

const transport = new HttpTransport({ baseUrl: "/api/" });

export const sessionsQuery = () =>
  queryOptions({
    queryKey: ["agent-harness", "sessions"],
    queryFn: ({ signal }) => transport.listSessions(signal)
  });

export function Sessions() {
  const queryClient = useQueryClient();
  const sessions = useQuery(sessionsQuery());
  const start = useMutation({
    mutationFn: () => startSession(transport, TicTacToeAgent),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: sessionsQuery().queryKey })
  });
  return (
    <>
      <button onClick={() => start.mutate()} disabled={start.isPending}>New session</button>
      {sessions.data?.map((s) => <a key={s.workflow_id} href={`?session=${s.workflow_id}`}>{s.label}</a>)}
    </>
  );
}
```

## Rendering replies as Markdown

A reply is model output, so treat its Markdown as untrusted. With `@tanstack/markdown`, which
escapes raw HTML by default, use `urlTransform` for two more rules:
- keep links only to `http:`, `https:` and `mailto:`;
- render images as their alt text. The renderer loads remote images by default, so a reply could
  plant a tracking pixel or an image URL that carries data out.

```tsx
import { useMemo } from "react";
import type { UrlTransform } from "@tanstack/markdown";
import { renderHtml } from "@tanstack/markdown/html";
import { parseMarkdown } from "@tanstack/markdown/parser";

const urlTransform: UrlTransform = (url, kind, defaultUrl) =>
  kind === "link" && defaultUrl && /^(https?:|mailto:)/i.test(defaultUrl) ? defaultUrl : null;

export function Reply({ text }: { text: string }) {
  // Once per render, which the session batches per flush, not once per streamed character.
  const html = useMemo(() => renderHtml(parseMarkdown(text, { urlTransform })), [text]);
  return <div className="reply" dangerouslySetInnerHTML={{ __html: html }} />;
}
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

```tsx
import { useMemo, type ComponentType } from "react";
import {
  parsePartialJson,
  type AgentSchema,
  type HandlerName,
  type HandlerOutput,
  type HarnessMessage
} from "@temporalio/agent-harness-client";
import type { TicTacToeAgent } from "./client_sdk/TicTacToeAgent"; // from harness-codegen

type Cards<A extends AgentSchema> = {
  [H in HandlerName<A>]?: ComponentType<{ output: Partial<HandlerOutput<A, H>> }>;
};

const cards: Cards<TicTacToeAgent> = {
  play: ({ output }) => <p className="move">{output.text}</p>
};

/** The reply text streamed so far: a data handler streams its output as JSON. */
const streamed = (message: HarnessMessage<TicTacToeAgent>) =>
  message.parts.map((part) => (part.type === "reply_delta" ? part.text : "")).join("");

export function Reply({ message }: { message: HarnessMessage<TicTacToeAgent> }) {
  // TypeScript can't tie the lookup to the narrowed message, so it is cast once here.
  const Card = cards[message.handler] as ComponentType<{ output: unknown }> | undefined;
  const text = message.output == null && Card ? streamed(message) : "";
  // Once per flush; strip a ```json fence a model may open with.
  const draft = useMemo(() => (text ? parsePartialJson(text.replace(/^\s*```(?:json)?\s*/, "")) : undefined), [text]);
  const output = message.output ?? draft;
  if (output == null) return null;
  return Card ? <Card output={output} /> : <pre>{JSON.stringify(output, null, 2)}</pre>;
}
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

```tsx
import { useState, type FormEvent } from "react";
import { buildPayload, describeSchema, emptyValues, validate, type SchemaField } from "@temporalio/agent-harness-client";
import { useAgentSession } from "@temporalio/agent-harness-react";

/** One entry of `GET /api/agent-interface/{sessionId}`. */
type Handler = { name: string; parameters: Record<string, unknown> };

function Input({ field, value, onChange }: { field: SchemaField; value: unknown; onChange: (value: unknown) => void }) {
  const common = { id: field.name, required: field.required && field.kind !== "boolean" };
  switch (field.kind) {
    case "boolean":
      return <input {...common} type="checkbox" checked={value === true} onChange={(e) => onChange(e.target.checked)} />;
    case "enum":
      return (
        <select {...common} value={String(value)} onChange={(e) => onChange(e.target.value)}>
          {field.choices?.map((choice) => <option key={choice}>{choice}</option>)}
        </select>
      );
    case "integer":
    case "number":
      return (
        <input {...common} type="number" step={field.kind === "integer" ? 1 : "any"} min={field.minimum} max={field.maximum}
          value={String(value)} onChange={(e) => onChange(e.target.value)} />
      );
    default:
      // Text. `pattern` is anchored by the browser but not by JSON Schema, hence the `.*`s. Give
      // "json", "array" and "object" fields a textarea or recurse into `field.fields`/`field.item`.
      return (
        <input {...common} minLength={field.minLength} maxLength={field.maxLength}
          pattern={field.pattern && `.*(?:${field.pattern}).*`} value={String(value)} onChange={(e) => onChange(e.target.value)} />
      );
  }
}

/** Key it on `handler.name`, so another handler starts from its own blank values. */
export function HandlerForm({ sessionId, handler }: { sessionId: string; handler: Handler }) {
  const agent = useAgentSession({ sessionId });
  const [fields] = useState(() => describeSchema(handler.parameters));
  const [values, setValues] = useState(() => emptyValues(fields));
  const [problems, setProblems] = useState<Record<string, string>>({});
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const found = validate(fields, values);
    setProblems(found);
    if (Object.keys(found).length === 0) void agent.sendMessage(handler.name, buildPayload(fields, values));
  };
  return (
    <form onSubmit={submit}>
      {fields.map((field) => (
        <p key={field.name}>
          <label htmlFor={field.name}>{field.title}</label>
          <Input field={field} value={values[field.name]} onChange={(value) => setValues({ ...values, [field.name]: value })} />
          {problems[field.name] && <span role="alert">{problems[field.name]}</span>}
        </p>
      ))}
      <button>Send</button>
    </form>
  );
}
```

With [TanStack Form](https://tanstack.com/form), wrap `validate` as a
[Standard Schema](https://standardschema.dev), which TanStack Form accepts as a validator and
whose issue paths it maps onto fields. The same `Input` renders each field:

```tsx
import { useForm, type StandardSchemaV1 } from "@tanstack/react-form";

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

export function HandlerTanStackForm({ sessionId, handler }: { sessionId: string; handler: Handler }) {
  const agent = useAgentSession({ sessionId });
  const [fields] = useState(() => describeSchema(handler.parameters));
  const form = useForm({
    defaultValues: emptyValues(fields),
    validators: { onSubmit: standardSchema(fields) },
    onSubmit: ({ value }) => agent.sendMessage(handler.name, buildPayload(fields, value))
  });
  return (
    <form onSubmit={(event) => { event.preventDefault(); void form.handleSubmit(); }}>
      {fields.map((schemaField) => (
        <form.Field key={schemaField.name} name={schemaField.name}>
          {(field) => (
            <p>
              <label htmlFor={schemaField.name}>{schemaField.title}</label>
              <Input field={schemaField} value={field.state.value} onChange={field.handleChange} />
              {field.state.meta.errors.map((issue) => <span key={issue?.message} role="alert">{issue?.message}</span>)}
            </p>
          )}
        </form.Field>
      ))}
      <button>Send</button>
    </form>
  );
}
```

## A virtual transcript

For long sessions, render only the visible messages with `@tanstack/react-virtual`:
- `anchorTo: "end"` keeps the view on the latest message as replies grow.
- `followOnAppend` follows new messages only while the reader is already at the end.
- `getItemKey` uses `message.key`, so a send keeps its measured height when the server admits it.

```tsx
import { useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

export function Transcript({ sessionId }: { sessionId: string }) {
  const agent = useAgentSession({ sessionId });
  const messages = [...agent.messages, ...agent.pending];
  const scroller = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: messages.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => 72,
    getItemKey: (index) => messages[index]!.key,
    anchorTo: "end",
    followOnAppend: true,
    overscan: 6
  });
  return (
    <div ref={scroller} tabIndex={-1} style={{ height: 600, overflow: "auto" }}>
      <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {virtualizer.getVirtualItems().map((item) => (
          <div
            key={item.key}
            ref={virtualizer.measureElement}
            data-index={item.index}
            style={{ position: "absolute", width: "100%", transform: `translateY(${item.start}px)` }}
          >
            <Message message={messages[item.index]!} />
          </div>
        ))}
      </div>
    </div>
  );
}
```

A row scrolled out of the window unmounts, taking focus with it. When that happens, move focus to
the row now at the same index, or to the list itself (hence its `tabIndex={-1}`).

## With TanStack Store

A session's state can also feed TanStack Store's `useSelector`, which reads any `get()` and
`subscribe()` source. Hold the session open with `useAgentSession` (or the selector hook)
somewhere under the same `<HarnessProvider store={store}>`:

```tsx
import { useSelector } from "@tanstack/react-store";

const state = store.entry({ sessionId }).state;
const source = {
  get: state.getSnapshot,
  subscribe: (listener: (snapshot: SessionSnapshot) => void) => ({
    unsubscribe: state.subscribe(() => listener(state.getSnapshot()))
  })
};
const busy = useSelector(source, (s) => s.agents[s.rootAgentId ?? ""]?.agentStatus === "busy");
```

## Development

This package is part of the pnpm workspace in `typescript/`, and builds and tests against the
`@temporalio/agent-harness-client` in the working tree; its published dependency is the
`^<version>` range in `package.json`. `bazel test //typescript/packages/react/...` builds and
tests it. Without Bazel, build the client first, then run these commands from
`typescript/packages/react` (see
[Testing local changes](../README.md#testing-local-changes-without-publishing)):

```sh
pnpm install
pnpm run check && pnpm test && pnpm run build
```
