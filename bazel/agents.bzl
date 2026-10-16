"""Agents' schema documents and the agents.toml registries that point at them, generated.

harness_agent writes one agent's schema with the temporal-agent-harness CLI;
harness_agent_registry writes an agents.toml for a package's agents and keeps it and their
schemas committed beside the BUILD file, so `temporal-agent-harness serve` reads the agents'
start interfaces without importing their code.
"""

load("//bazel:binary.bzl", "harness_module_binary")
load("//bazel:committed_files.bzl", "committed_files")

AgentInfo = provider(
    doc = "One agent's schema document and, when it is launchable from a registry, its entry's fields.",
    fields = {
        "description": "What the agent does, for the UI.",
        "key": "The registry key; empty when the agent has no registry entry.",
        "label": "The name the UI shows.",
        "schema": "The agent's schema document (File).",
        "task_queue": "The task queue its worker polls.",
    },
)

_CLI_LIB = Label("//temporal_agent_harness/web:cli_lib")
_CLI_MODULE = "temporal_agent_harness.web.cli"

def _write_schema(ctx, tool, args, env = {}):
    # Not `<name>.schema.json`: that is where the committed copy is copied into the output tree.
    out = ctx.actions.declare_file(ctx.label.name + ".generated.schema.json")
    ctx.actions.run(
        outputs = [out],
        executable = tool,
        arguments = args + ["-o", out.path],
        env = env,
        mnemonic = "HarnessSchema",
        progress_message = "Writing the schema for %{label}",
    )
    return out

def _harness_schema_impl(ctx):
    out = _write_schema(ctx, ctx.executable.tool, ctx.attr.args)
    return [DefaultInfo(files = depset([out]))]

harness_schema = rule(
    implementation = _harness_schema_impl,
    doc = "`<name>.generated.schema.json`: what `<tool> <args...> -o <file>` writes.",
    attrs = {
        "args": attr.string_list(mandatory = True, doc = 'The CLI\'s arguments, e.g. ["schema", "--protocol"].'),
        "tool": attr.label(
            default = "//temporal_agent_harness/web:cli",
            executable = True,
            cfg = "exec",
            doc = "The temporal-agent-harness CLI.",
        ),
    },
)

def _harness_agent_rule_impl(ctx):
    schema = _write_schema(ctx, ctx.executable.tool, ["schema", ctx.attr.agent], ctx.attr.env)
    return [
        DefaultInfo(files = depset([schema])),
        AgentInfo(
            description = ctx.attr.description,
            key = ctx.attr.key,
            label = ctx.attr.agent_label,
            schema = schema,
            task_queue = ctx.attr.task_queue,
        ),
    ]

_harness_agent_rule = rule(
    implementation = _harness_agent_rule_impl,
    attrs = {
        "agent": attr.string(mandatory = True),
        "agent_label": attr.string(),
        "description": attr.string(),
        "env": attr.string_dict(),
        "key": attr.string(),
        "task_queue": attr.string(),
        "tool": attr.label(mandatory = True, executable = True, cfg = "exec"),
    },
)

def _harness_agent_impl(name, visibility, agent, deps, env, key, task_queue, label, description):
    entry = [key, task_queue, label, description]
    if any(entry) and not all(entry):
        fail("harness_agent: set all of key, task_queue, label, and description (a registry entry), or none")

    # The CLI, able to import this agent: only the schema is written with it.
    harness_module_binary(
        name = name + "_schema_tool",
        module = _CLI_MODULE,
        deps = [_CLI_LIB] + deps,
    )
    _harness_agent_rule(
        name = name,
        visibility = visibility,
        agent = agent,
        agent_label = label or "",
        description = description or "",
        env = env,
        key = key or "",
        task_queue = task_queue or "",
        tool = ":" + name + "_schema_tool",
    )

harness_agent = macro(
    doc = """One agent: its schema document (`temporal-agent-harness schema <agent>`), which a
    harness_agent_registry commits as `<name>.schema.json`, and, for an agent a registry lists,
    its entry's fields. The entry's
    workflow_type is the workflow the schema describes. An agent only ever run as a subagent
    leaves the entry's fields unset.
    """,
    implementation = _harness_agent_impl,
    attrs = {
        "agent": attr.string(mandatory = True, configurable = False, doc = "The agent's class, `module.path:ClassName`."),
        "deps": attr.label_list(mandatory = True, configurable = False, doc = "Libraries that define the class."),
        "env": attr.string_dict(configurable = False, doc = "Environment the class's module needs to import, e.g. a placeholder API key it reads at import time."),
        "key": attr.string(configurable = False, doc = "The registry key."),
        "task_queue": attr.string(configurable = False, doc = "The task queue its worker polls."),
        "label": attr.string(configurable = False, doc = "The name the UI shows."),
        "description": attr.string(configurable = False, doc = "What the agent does, for the UI."),
    },
)

def _registry_rule_impl(ctx):
    agents = [agent[AgentInfo] for agent in ctx.attr.agents]
    for target, agent in zip(ctx.attr.agents, agents):
        if not agent.key:
            fail("%s has no registry entry: give it key, task_queue, label, and description" % target.label)
    spec = ctx.actions.declare_file(ctx.label.name + ".spec.json")
    ctx.actions.write(spec, json.encode({
        "agents": [
            {
                "description": agent.description,
                "key": agent.key,
                "label": agent.label,
                # The committed copy's name (harness_agent_registry), beside agents.toml.
                "schema": target.label.name + ".schema.json",
                "schema_path": agent.schema.path,
                "task_queue": agent.task_queue,
            }
            for target, agent in zip(ctx.attr.agents, agents)
        ],
        "registry": ctx.attr.registry,
        "update": ctx.attr.update,
    }))
    out = ctx.actions.declare_file(ctx.label.name + ".generated.toml")
    ctx.actions.run(
        inputs = [spec] + [agent.schema for agent in agents],
        outputs = [out],
        executable = ctx.executable._writer,
        arguments = [spec.path, out.path],
        mnemonic = "HarnessRegistry",
        progress_message = "Writing the agent registry %{label}",
    )
    return [DefaultInfo(files = depset([out]))]

_registry_rule = rule(
    implementation = _registry_rule_impl,
    attrs = {
        "agents": attr.label_list(mandatory = True, providers = [AgentInfo]),
        "registry": attr.string(mandatory = True),
        "update": attr.string(mandatory = True),
        "_writer": attr.label(default = "//bazel:agents_toml", executable = True, cfg = "exec"),
    },
)

def _harness_agent_registry_impl(name, visibility, agents, srcs):
    _registry_rule(
        name = name + "_toml",
        agents = agents,
        registry = "//%s:%s" % (native.package_name(), name),
        update = "//%s:%s_update" % (native.package_name(), name),
    )

    # Each schema's name is its harness_agent's, which the registry's `schema` entries use too.
    committed_files(
        name = name,
        visibility = visibility,
        srcs = srcs,
        generated = {"agents.toml": ":%s_toml" % name} | {
            agent.name + ".schema.json": agent
            for agent in agents
        },
    )

harness_agent_registry = macro(
    doc = """This package's agents.toml for `agents` (harness_agent targets), committed beside the
    BUILD file with each agent's `<name>.schema.json`, which its `schema` entries point at.

    `bazel run :<name>_update` writes them; `<name>_toml_up_to_date_test` and
    `<name>_<Agent>_schema_json_up_to_date_test` fail while one is out of date (committed_files).
    `srcs` lists the committed files: `agents.toml` and each agent's `<name>.schema.json`.
    """,
    implementation = _harness_agent_registry_impl,
    attrs = {
        "agents": attr.label_list(mandatory = True, configurable = False, doc = "harness_agent targets, in the order of their entries."),
        "srcs": attr.label_list(mandatory = True, allow_files = True, configurable = False, doc = "The committed agents.toml and each agent's schema document."),
    },
)
