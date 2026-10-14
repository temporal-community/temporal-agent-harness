"""TypeScript types generated from `temporal-agent-harness schema` documents, kept in the source tree.

harness_types commits one file (e.g. packages/client's event protocol); harness_client_sdk commits
a typed client for each of a package's agents.
"""

load("//bazel:committed_files.bzl", "committed_files")

def _types_rule_impl(ctx):
    # harness-codegen is a js_binary: it runs from the output root, so paths are relative to it.
    out = ctx.actions.declare_file(ctx.label.name + ".ts")
    ctx.actions.run(
        inputs = [ctx.file.schema],
        outputs = [out],
        executable = ctx.executable._codegen,
        arguments = ctx.attr.codegen_args + [ctx.file.schema.short_path, "-o", out.short_path],
        env = {"BAZEL_BINDIR": ctx.bin_dir.path},
        mnemonic = "HarnessCodegen",
        progress_message = "Generating TypeScript types for %{label}",
    )
    return [DefaultInfo(files = depset([out]))]

# `<name>.ts`: `harness-codegen [codegen_args] <schema>`.
_types_rule = rule(
    implementation = _types_rule_impl,
    attrs = {
        "codegen_args": attr.string_list(),
        "schema": attr.label(allow_single_file = [".json"], mandatory = True),
        "_codegen": attr.label(
            default = "//packages/codegen:cli",
            executable = True,
            cfg = "exec",
        ),
    },
)

def _harness_types_impl(name, visibility, src, schema, codegen_args):
    _types_rule(
        name = name + "_ts",
        visibility = visibility,
        codegen_args = codegen_args,
        schema = schema,
    )
    committed_files(
        name = name,
        visibility = visibility,
        srcs = [src],
        generated = {src.name: ":%s_ts" % name},
    )

harness_types = macro(
    doc = """TypeScript types (packages/codegen) for a schema document, committed as `src`.

    `bazel run :<name>_update` writes `src`; `<name>_<file>_up_to_date_test` fails while it is out
    of date. `:<name>_ts` is the generated file.
    """,
    implementation = _harness_types_impl,
    attrs = {
        "src": attr.label(mandatory = True, allow_single_file = [".ts"], configurable = False, doc = "The committed file."),
        "schema": attr.label(mandatory = True, configurable = False, doc = "The schema document: a harness_agent or harness_schema target."),
        "codegen_args": attr.string_list(configurable = False, doc = 'Extra harness-codegen flags, e.g. ["--protocol"].'),
    },
)

def _harness_client_sdk_impl(name, visibility, agents, srcs):
    generated = {}
    for agent in agents:
        types = "%s_%s_ts" % (name, agent.name)
        _types_rule(
            name = types,
            visibility = visibility,
            schema = agent,
        )
        generated["%s/%s.ts" % (name, agent.name)] = ":" + types
    committed_files(
        name = name,
        visibility = visibility,
        srcs = srcs,
        generated = generated,
    )

harness_client_sdk = macro(
    doc = """Typed TypeScript clients for `agents` (harness_agent targets), committed as
    `<name>/<Agent>.ts` (packages/codegen's output for each agent's schema).

    `bazel run :<name>_update` writes them; `<name>_<Agent>_ts_up_to_date_test` fails while one is
    out of date. `:<name>_<Agent>_ts` is an agent's generated types.
    """,
    implementation = _harness_client_sdk_impl,
    attrs = {
        "agents": attr.label_list(mandatory = True, configurable = False, doc = "harness_agent targets."),
        "srcs": attr.label_list(mandatory = True, allow_files = True, configurable = False, doc = "The committed `<name>/<Agent>.ts` files."),
    },
)
