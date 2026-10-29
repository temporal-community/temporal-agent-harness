"""static_page: a page with no build step, served with the npm packages it imports, built by Bazel."""

load("@aspect_rules_js//js:defs.bzl", "js_binary", "js_test")
load("@bazel_lib//lib:copy_to_directory.bzl", "copy_to_directory")

def _static_page_impl(name, visibility, srcs, packages, port):
    # An npm_package's output is the directory named for its target, which lands at its workspace
    # path; move each one under its directory name, beside the page's own files.
    copy_to_directory(
        name = name + "_files",
        srcs = srcs + list(packages.keys()),
        replace_prefixes = {
            "%s/%s/" % (package.package, package.name): directory + "/"
            for package, directory in packages.items()
        },
        visibility = visibility,
    )

    js_binary(
        name = name,
        data = [":%s_files" % name],
        entry_point = Label(":static_page_server"),
        args = [
            "$(rootpath :%s_files)" % name,
            str(port),
        ],
        visibility = visibility,
    )

    js_test(
        name = name + "_imports_resolve_test",
        data = [":%s_files" % name],
        entry_point = Label(":static_page_imports_test"),
        args = ["$(rootpath :%s_files)" % name],
    )

static_page = macro(
    doc = """A page the browser loads as-is, with the npm packages it imports beside it.

    `<name>_files` is the page's directory: `srcs` at its root and each of `packages` (an
    `npm_package`, laid out as npm publishes it) under its directory name. `bazel run :<name>`
    serves it on http://127.0.0.1:<port>/ (bazel/static_page_server.mjs), or on the port given
    after `--` (`bazel run :<name> -- 5174`).

    `<name>_imports_resolve_test` checks that every module the page loads is there
    (bazel/static_page_imports_test.mjs): each local path index.html's import map names, each
    relative import in its files, and each bare import, which the import map must cover.
    """,
    implementation = _static_page_impl,
    attrs = {
        "srcs": attr.label_list(mandatory = True, allow_files = True, configurable = False, doc = "The page's own files: index.html and what it loads."),
        "packages": attr.label_keyed_string_dict(configurable = False, doc = "npm_package targets, each to the directory it is served from."),
        "port": attr.int(default = 5173, configurable = False, doc = "The port `bazel run` serves on unless given one."),
    },
)
