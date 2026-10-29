import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { z } from "zod";
import {
  InMemoryFileSystem,
  Mount,
  codeModeTool,
  injected,
  tool as harnessTool,
  type AnyHarnessTool,
} from "@temporalio/agent-harness";
import {
  CodeModeStubError,
  renderTypeCheckStubs,
} from "@temporalio/agent-harness/harness/code-mode";
import { searchItems } from "../../fixtures/workflows.ts";
import { beta, stubTool as tool } from "./tools.ts";

describe("codeModeTool", () => {
  it("is one inline tool taking a script and returning text", () => {
    const t = codeModeTool([searchItems, beta], { name: "run_stuff" });
    assert.equal(t.name, "run_stuff");
    assert.equal(t.kind, "inline");
    assert.deepEqual(Object.keys(t.parameters.properties as object), ["script"]);
    assert.equal(t.inherentlySafe, true);
  });

  it("its description embeds the contract and the host interface", () => {
    const { description } = codeModeTool([searchItems, beta], { name: "run_stuff" });
    assert.ok(description.includes("asyncio.run(main())"));
    assert.ok(description.includes("async def search(origin: str, n: int) -> SearchResp"));
    assert.ok(description.includes("async def beta(x: int) -> int"));
    assert.ok(description.includes("Search the catalog for items."));
  });

  it("rejects an empty tool list and duplicate host-function names", () => {
    assert.throws(() => codeModeTool([], { name: "run_x" }), /at least one tool or mount/);
    assert.throws(
      () =>
        codeModeTool(
          [tool("dup", z.object({ a: z.string() })), tool("dup", z.object({ b: z.string() }))],
          { name: "run_x" },
        ),
      { message: "duplicate host-function name 'dup' in this codeModeTool." },
    );
  });

  it("accepts injected parameters and hides them from the interface", () => {
    const readPage = harnessTool({
      name: "read_page",
      description: "Read a page.",
      inputSchema: z.object({ page_url: z.string() }),
      outputSchema: z.string(),
      injected: { store: injected<string>() },
      execute: async () => "",
    }) as unknown as AnyHarnessTool;
    assert.equal(
      codeModeTool([readPage], { name: "run_x", injections: { store: "S" } }).name,
      "run_x",
    );
    const src = renderTypeCheckStubs([readPage]).source;
    assert.ok(src.includes("async def read_page(page_url: str) -> str: ..."));
    assert.ok(!src.includes("store"));
  });

  it("rejects a tool with no typed output", () => {
    assert.throws(
      () => codeModeTool([tool("untyped", z.object({ a: z.string() }), null)], { name: "run_x" }),
      CodeModeStubError,
    );
  });

  it("distinct names allow several Code Mode tools over overlapping sets, each carrying its stubs", () => {
    const a = codeModeTool([searchItems, beta], { name: "run_a" });
    const b = codeModeTool([beta], { name: "run_b" });
    assert.deepEqual([a.name, b.name], ["run_a", "run_b"]);
    assert.deepEqual(b.codeModeStubs, renderTypeCheckStubs([beta]));
  });
});

describe("what the model is told about the filesystem", () => {
  const search = tool("search", z.object({ q: z.string() }), z.string(), "Search.");

  it("lists each mount with its access and description, and declares open() for the checker", () => {
    const codeTool = codeModeTool([], {
      name: "run_code",
      mounts: [
        new Mount("/skills", new InMemoryFileSystem(), { readOnly: true, description: "Skills." }),
        new Mount("/workspace", new InMemoryFileSystem()),
      ],
    });
    assert.ok(codeTool.description.includes("It has the filesystem listed under Filesystem below"));
    assert.ok(
      codeTool.description.includes(
        "Host functions available:\n\n(none)\n\nFilesystem:\n\n- `/skills` (read-only): Skills.\n- `/workspace` (writable)\n",
      ),
    );
    assert.ok(codeTool.codeModeStubs.source.includes("def open(file: str | _os.PathLike[str]"));
  });

  it("a tool without mounts says the sandbox has no filesystem and declares no open()", () => {
    const plain = codeModeTool([search], { name: "run_plain" });
    assert.ok(
      plain.description.includes(
        "The sandbox has no filesystem, no network, and no imports except `asyncio`",
      ),
    );
    assert.ok(!plain.description.includes("Filesystem:"));
    assert.ok(!plain.codeModeStubs.source.includes("def open("));
  });
});
