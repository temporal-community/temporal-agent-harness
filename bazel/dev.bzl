"""harness_dev: the example programs to run together during development, chosen at run time."""

load("@bazel_skylib//lib:shell.bzl", "shell")

def _env(target):
    return target[RunEnvironmentInfo].environment if RunEnvironmentInfo in target else {}

def _program(target):
    # Paths are relative to the runfiles root, where `bazel run` starts the runner.
    return {
        "env": _env(target),
        "path": target[DefaultInfo].files_to_run.executable.short_path,
    }

def _harness_dev_rule_impl(ctx):
    instructions = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(instructions, json.encode_indent({
        "cli": _program(ctx.attr.cli) if ctx.attr.cli else None,
        "examples": ctx.attr.examples,
        "server": ctx.attr.server,
        "session_manager": ctx.attr.session_manager,
        "workers": {example: _program(worker) for worker, example in ctx.attr.workers.items()},
    }))

    runner = ctx.attr._runner
    launcher = ctx.actions.declare_file(ctx.label.name + ".sh")
    ctx.actions.write(
        launcher,
        "\n".join(
            [
                "#!/usr/bin/env bash",
                # The runner and every program it starts find their files in this target's
                # runfiles, which hold all of theirs.
                'export RUNFILES_DIR="${RUNFILES_DIR:-$0.runfiles}"',
            ] +
            ["export %s=%s" % (k, shell.quote(v)) for k, v in _env(runner).items()] +
            ['exec %s %s "$@"' % (
                shell.quote(ctx.executable._runner.short_path),
                shell.quote(instructions.short_path),
            )],
        ) + "\n",
        is_executable = True,
    )

    runfiles = ctx.runfiles([instructions, ctx.executable._runner])
    for target in [runner] + ([ctx.attr.cli] if ctx.attr.cli else []) + ctx.attr.workers.keys():
        runfiles = runfiles.merge(target[DefaultInfo].default_runfiles)
    return [DefaultInfo(executable = launcher, runfiles = runfiles)]

_harness_dev_rule = rule(
    implementation = _harness_dev_rule_impl,
    executable = True,
    attrs = {
        "cli": attr.label(executable = True, cfg = "target"),
        "examples": attr.string_list(mandatory = True),
        "server": attr.bool(),
        "session_manager": attr.bool(),
        "workers": attr.label_keyed_string_dict(cfg = "target"),
        "_runner": attr.label(default = "//bazel:dev_runner", executable = True, cfg = "target"),
    },
)

def _harness_dev_impl(name, visibility, examples, cli, session_manager, server, workers):
    if (session_manager or server) and not cli:
        fail("harness_dev: the session manager and server run `cli`")
    _harness_dev_rule(
        name = name,
        visibility = visibility,
        cli = cli,
        examples = examples,
        server = server,
        session_manager = session_manager,
        workers = {"//examples/%s:worker" % example: example for example in examples} if workers else {},
    )

harness_dev = macro(
    doc = """Runs examples' programs together: `bazel run <name> -- [example ...]`.

    For each example named at run time (its directory under examples/, or that path's last
    segment; none means all of `examples`), runs its `:worker` when `workers` is set, plus `cli`'s
    session manager and `cli serve` over the chosen examples' agents.toml registries when
    `session_manager` and `server` are set. Every output line carries its program's label, and
    Ctrl-C stops them all (bazel/dev_runner.py).
    """,
    implementation = _harness_dev_impl,
    attrs = {
        "examples": attr.string_list(mandatory = True, configurable = False, doc = "Directories under examples/ that can be run."),
        "cli": attr.label(configurable = False, doc = "The temporal-agent-harness CLI, able to import every example's agents."),
        "session_manager": attr.bool(configurable = False, doc = "Run `cli session-manager`."),
        "server": attr.bool(configurable = False, doc = "Run `cli serve` over the chosen examples' registries on :8000."),
        "workers": attr.bool(configurable = False, doc = "Run each chosen example's `:worker`."),
    },
)
