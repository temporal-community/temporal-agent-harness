"""vitest_test: one vitest test file, run from its workspace package's root like `pnpm test`."""

load("@npm//typescript/packages/react:vitest/package_json.bzl", react_vitest = "bin")
load("@npm//typescript/packages/svelte:vitest/package_json.bzl", svelte_vitest = "bin")

# Each workspace package's own vitest (rules_js generates a bin per package that depends on it).
# A package's tests run with its vitest, its config and its dependencies' versions.
_VITEST = {
    "typescript/packages/react": react_vitest,
    "typescript/packages/svelte": svelte_vitest,
}

def _vitest_test_impl(name, visibility, src, deps, **kwargs):
    package = native.package_name()
    root = [path for path in _VITEST if package == path or package.startswith(path + "/")]
    if not root:
        fail("vitest_test %s: %s is in no package bazel/vitest.bzl knows; add its vitest there" % (name, package))
    root = root[0]

    _VITEST[root].vitest_test(
        name = name,
        visibility = visibility,
        args = [
            "run",
            # vitest takes the file relative to the package root, where its config is.
            (package + "/" + src.name).removeprefix(root + "/"),
        ],
        chdir = root,
        # js_test has no deps: vitest compiles the test and its imports when it runs, so they are
        # runfiles like anything else it reads.
        data = [
            src,
            "//%s:vitest_config" % root,
        ] + deps,
        **kwargs
    )

vitest_test = macro(
    doc = """Runs one vitest test file the way `pnpm test` does in its package: from the package
    root, with the package's vitest and `:vitest_config` (vitest.config.ts and whatever it reads).

    Declare it beside the test file; `deps` is what the file imports (source targets and
    `node_modules` packages).
    """,
    implementation = _vitest_test_impl,
    inherit_attrs = "common",
    attrs = {
        "src": attr.label(mandatory = True, allow_single_file = True, configurable = False, doc = "The test file, in this package."),
        "deps": attr.label_list(doc = "What the test file imports."),
    },
)
