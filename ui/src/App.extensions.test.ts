import { describe, expect, it } from "vitest";
import { render } from "svelte/server";
import App from "./App.svelte";
import HeaderControlExtension from "./test/fixtures/HeaderControlExtension.svelte";
import WorkspaceExtension from "./test/fixtures/WorkspaceExtension.svelte";

describe("App extensions", () => {
  it("renders the supplied header control in the top bar", () => {
    const { body } = render(App, {
      props: {
        extensions: { headerControl: HeaderControlExtension }
      }
    });

    expect(body).toContain("data-header-control-extension");
  });

  it("forwards a supplied workspace extension with neutral chat props", () => {
    const { body } = render(App, {
      props: {
        extensions: { workspaceComponent: WorkspaceExtension }
      }
    });

    expect(body).toContain("data-workspace-extension");
    expect(body).toMatch(
      /session=[^;]+;following=(?:true|false);closed=(?:true|false);send=function/
    );
  });
});
