import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { z } from "zod";
import {
  ToolApprovalPolicy,
  defineAgent,
  handler,
  subagentToolset,
  type AnyHarnessTool,
} from "@temporalio/agent-harness";
import {
  pyRepr,
  renderHostInterface,
  renderTypeCheckStubs,
} from "@temporalio/agent-harness/harness/code-mode";
import { searchItems } from "../../fixtures/workflows.ts";
import { beta, pack, stubTool as tool } from "./tools.ts";

const stub = (...tools: AnyHarnessTool[]) => renderTypeCheckStubs(tools).source;
const classNames = (src: string) =>
  [...src.matchAll(/^class (\w+)\(TypedDict\):/gm)].map((m) => m[1]);

describe("stubs from JSON Schema", () => {
  it("renders primitives with no TypedDicts", () => {
    const src = stub(
      tool("do", z.object({ a: z.string(), b: z.number().int(), c: z.number(), d: z.boolean() })),
    );
    assert.deepEqual(classNames(src), []);
    assert.ok(src.includes("async def do(a: str, b: int, c: float, d: bool) -> str: ..."));
    assert.ok(src.startsWith("from __future__ import annotations\n"));
  });

  it("renders a nested model parameter and a list-of-model return", () => {
    const SearchReq = z
      .object({ origin: z.string(), n: z.number().int() })
      .meta({ title: "SearchReq" });
    const Item = z.object({ id: z.string(), price: z.number() }).meta({ title: "Item" });
    const src = stub(
      tool(
        "search",
        z.object({ request: SearchReq }),
        z.object({ items: z.array(Item) }).meta({ title: "SearchResp" }),
      ),
    );
    assert.deepEqual(new Set(classNames(src)), new Set(["SearchReq", "SearchResp", "Item"]));
    assert.ok(src.includes("items: list[Item]"));
    assert.ok(src.includes("async def search(request: SearchReq) -> SearchResp: ..."));
  });

  it("renders nullable and union types as PEP 604, and dicts with key and value", () => {
    const src = stub(
      tool(
        "f",
        z.object({
          a: z.number().int().nullable(),
          b: z.union([z.number().int(), z.string()]),
          m: z.record(z.string(), z.number().int()),
        }),
      ),
    );
    assert.ok(
      src.includes("async def f(a: int | None, b: int | str, m: dict[str, int]) -> str: ..."),
    );
  });

  it("emits a shared model once, and gives colliding names distinct TypedDicts", () => {
    const Req = z.object({ q: z.string() }).meta({ title: "SearchReq" });
    const shared = stub(tool("a", z.object({ req: Req })), tool("b", z.object({ req: Req })));
    assert.deepEqual(classNames(shared), ["SearchReq"]);
    const collided = classNames(
      stub(
        tool("a", z.object({ req: z.object({ x: z.number() }).meta({ title: "Dup" }) })),
        tool("b", z.object({ req: z.object({ y: z.string() }).meta({ title: "Dup" }) })),
      ),
    );
    assert.equal(collided.length, 2);
    assert.equal(new Set(collided).size, 2);
  });

  it("a recursive model terminates and references itself", () => {
    const Node: z.ZodType<{ value: number; children?: unknown[] }> = z
      .object({
        value: z.number().int(),
        get children() {
          return z.array(Node).optional();
        },
      })
      .meta({ title: "Node" });
    const src = stub(tool("f", z.object({ n: Node })));
    assert.deepEqual(classNames(src), ["Node"]);
    assert.ok(src.includes("children: NotRequired[list[Node]]"));
  });

  it("a defaulted field is not required on the way in, and the import is emitted", () => {
    const src = stub(
      tool(
        "f",
        z.object({
          s: z
            .object({ value: z.number().int(), tag: z.string().default("x") })
            .meta({ title: "Tagged" }),
        }),
      ),
    );
    assert.ok(src.includes("value: int"));
    assert.ok(!src.includes("value: NotRequired"));
    assert.ok(src.includes("tag: NotRequired[str]"));
    assert.ok(src.split("\n")[1]!.includes("NotRequired"));
  });

  it("renders an enum as a Literal of its values, and an explicit any as Any", () => {
    const src = stub(tool("f", z.object({ c: z.enum(["red", "green"]), x: z.any() })));
    assert.ok(src.includes('c: Literal["red", "green"]'));
    assert.ok(src.includes("x: Any"));
    assert.match(src, /from typing import .*Any.*Literal/);
  });

  it("names the TypedDicts it defines", () => {
    assert.deepEqual(renderTypeCheckStubs([pack]).type_names, ["Box"]);
    assert.ok(stub(pack).includes("class Box(TypedDict):"));
    assert.deepEqual(renderTypeCheckStubs([beta]).type_names, []);
  });

  it("the host interface attaches each tool's description as a docstring", () => {
    const iface = renderHostInterface([searchItems]);
    assert.ok(iface.includes('    """\n    Search the catalog for items.\n    """'));
    assert.ok(iface.includes("class SearchResp(TypedDict):"));
  });

  it("renders a subagent send tool with the child's real models", () => {
    const Question = z.object({ text: z.string() }).meta({ title: "Question" });
    const Answer = z.object({ text: z.string() }).meta({ title: "Answer" });
    const child = defineAgent({
      name: "ChildAgent",
      approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
      handlers: {
        ask: handler({
          description: "Answer a free-form question.",
          input: Question,
          output: Answer,
        }),
      },
      create: () => ({ ask: async () => ({ text: "" }) }),
    });
    const [, ask] = subagentToolset(child, { key: "child", taskQueue: "q" });
    const src = stub(ask!);
    assert.ok(src.includes("async def child_ask(subagent: str, message: Question) -> Answer: ..."));
    assert.deepEqual(new Set(classNames(src)), new Set(["Question", "Answer"]));
  });

  it("pyRepr renders JSON as Python would", () => {
    assert.equal(
      pyRepr({ a: [1, 2.5, true, null], "it's": "x\ny" }),
      `{'a': [1, 2.5, True, None], "it's": 'x\\ny'}`,
    );
    assert.equal(
      pyRepr(["a\tb\r\n", `it's "q"`, "c:\\d"]),
      `['a\\tb\\r\\n', 'it\\'s "q"', 'c:\\\\d']`,
    );
  });
});
