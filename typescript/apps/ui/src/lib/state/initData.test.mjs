// ABOUTME: An agent's init data as the console handles it: which agent boot may start without
// asking anyone, the form the data renders as, the payload a filled form becomes, and that boot
// and "New session" send (or refuse to guess) the data through the controller.

import assert from "node:assert/strict";
import { beforeAll, describe, it } from "vitest";

import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { AgentRunController } from "./agentRun.svelte.ts";
import { defaultBootAgent, initDataForm, initDataPayload, requiresInitData } from "./initData.ts";

const tripSchema = {
  title: "Trip",
  description: "Who is travelling, and where.",
  type: "object",
  properties: {
    traveler: { type: "string", title: "Traveler" },
    nights: { type: "integer", title: "Nights", default: 2, minimum: 1 }
  },
  required: ["traveler"]
};

const agent = (key, init_data = null) => ({
  key,
  label: key.toUpperCase(),
  workflow_type: `${key}-type`,
  task_queue: "q",
  description: "",
  init_data
});

const planner = agent("planner", { required: true, schema: tripSchema });
const greeter = agent("greeter", { required: false, schema: tripSchema });
const qa = agent("qa");

describe("defaultBootAgent", () => {
  it("prefers qa, and otherwise the first registered agent", () => {
    assert.equal(defaultBootAgent([greeter, qa])?.key, "qa");
    assert.equal(defaultBootAgent([greeter, planner])?.key, "greeter");
  });

  it("never picks an agent that requires init data, since boot has nobody to ask", () => {
    assert.equal(defaultBootAgent([planner, qa])?.key, "qa");
    assert.equal(defaultBootAgent([{ ...planner, key: "qa" }, greeter])?.key, "greeter");
    assert.equal(defaultBootAgent([planner]), null);
  });

  it("treats a server that does not report init data as none", () => {
    const { init_data: _, ...old } = qa;
    assert.equal(requiresInitData(old), false);
    assert.equal(defaultBootAgent([old])?.key, "qa");
  });
});

describe("initDataForm / initDataPayload", () => {
  it("renders the data model's schema, required or not", () => {
    const form = initDataForm(planner);
    assert.ok(form);
    assert.equal(form.required, true);
    assert.equal(form.title, "Trip");
    assert.equal(form.description, "Who is travelling, and where.");
    assert.deepEqual(
      form.fields.map((field) => [field.name, field.required]),
      [
        ["traveler", true],
        ["nights", false]
      ]
    );
    assert.equal(initDataForm(greeter)?.required, false);
    assert.equal(initDataForm(qa), null);
  });

  it("refuses a form missing a required field, and builds the payload once it is filled", () => {
    const form = initDataForm(planner);
    assert.deepEqual(initDataPayload(form), { problems: { traveler: "Required." } });
    form.values.traveler = "Ada";
    form.values.nights = "3";
    assert.deepEqual(initDataPayload(form), { data: { traveler: "Ada", nights: 3 } });
  });
});

describe("the controller's session starts", () => {
  beforeAll(() => {
    installBrowserSurface();
  });

  // eslint-disable-next-line require-yield
  async function* silent() {
    return;
  }

  function controllerWith(agents) {
    const created = [];
    const controller = new AgentRunController({
      async listAgents() {
        return { agents };
      },
      async acceptedMessageTypes() {
        return { accepted: [] };
      },
      async listSessions() {
        return [];
      },
      async createSession(request) {
        created.push(request);
        return {
          workflow_id: `wf-${created.length}`,
          agent_workflow_type: request.agent_workflow_type,
          label: "Session",
          created_at: 0,
          execution_status: "RUNNING",
          closed: false
        };
      },
      async agentInterface() {
        return [];
      },
      async operatorInterface() {
        return [];
      },
      async workflowStatus(workflowId) {
        return { workflow_id: workflowId, execution_status: "RUNNING", closed: false };
      },
      attach: () => silent()
    });
    return { controller, created };
  }

  it("boot starts an agent that does not require init data, without any", async () => {
    const { controller, created } = controllerWith([planner, greeter]);
    await controller.initialize();
    assert.deepEqual(created, [{ agent_workflow_type: "greeter-type" }]);
  });

  it("boot starts nothing when every agent requires init data, and says why", async () => {
    const { controller, created } = controllerWith([planner]);
    await controller.initialize();
    assert.deepEqual(created, []);
    assert.match(controller.connectionError ?? "", /needs init data/);
  });

  it("a new session sends the init data it was given", async () => {
    const { controller, created } = controllerWith([planner, qa]);
    await controller.startNewSession("planner-type", { traveler: "Ada" });
    assert.deepEqual(created.at(-1), {
      agent_workflow_type: "planner-type",
      data: { traveler: "Ada" }
    });
  });
});
