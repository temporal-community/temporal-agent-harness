"""harness_js_test: a js_test that can start the Temporal dev server pinned in MODULE.bazel."""

load("@aspect_rules_js//js:defs.bzl", "js_test")

_TEMPORAL_CLI = Label("//bazel/temporal:cli")

def _harness_js_test_impl(name, visibility, entry_point, data, env, size, timeout, tags, flaky):
    js_test(
        name = name,
        visibility = visibility,
        entry_point = entry_point,
        data = data + [_TEMPORAL_CLI],
        # An external repository's file, run from where it is rather than copied to bazel-bin.
        no_copy_to_bin = [_TEMPORAL_CLI],
        # Relative to the runfiles root; the test makes it absolute.
        env = env | {"TEMPORAL_CLI_PATH": "$(rootpath %s)" % _TEMPORAL_CLI},
        size = size,
        timeout = timeout or None,
        tags = tags,
        flaky = flaky,
    )

harness_js_test = macro(
    doc = """A node:test file run with the `temporal` CLI pinned in MODULE.bazel.

    TEMPORAL_CLI_PATH names the CLI, whose dev server the test starts instead of downloading one.
    """,
    implementation = _harness_js_test_impl,
    attrs = {
        "entry_point": attr.label(mandatory = True, allow_single_file = True, configurable = False, doc = "The compiled test file."),
        "data": attr.label_list(allow_files = True, configurable = False, doc = "The test's ts_project and anything else it reads at runtime."),
        "env": attr.string_dict(configurable = False, doc = "Extra environment variables."),
        "size": attr.string(configurable = False, default = "medium", values = ["small", "medium", "large", "enormous"]),
        "timeout": attr.string(configurable = False, values = ["", "short", "moderate", "long", "eternal"]),
        "tags": attr.string_list(configurable = False),
        "flaky": attr.bool(configurable = False),
    },
)
