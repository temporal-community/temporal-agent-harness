"""published_npm_package: a workspace package laid out as npm publishes it, and its tarball."""

load("@aspect_rules_js//js:defs.bzl", "js_test")
load("@aspect_rules_js//npm:defs.bzl", "npm_package")

def _pnpm_pack_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name.removesuffix("_tgz") + ".tgz")
    pnpm = ctx.attr._pnpm[DefaultInfo].files_to_run

    # Bazel's outputs are executable, and `pnpm pack` keeps a file's executable bit, so the copy
    # drops it (none of these packages ships an executable). pnpm, a js_binary, runs from the
    # output root (BAZEL_BINDIR), so it gets absolute paths.
    ctx.actions.run_shell(
        inputs = [ctx.file.package],
        outputs = [out],
        tools = [pnpm],
        command = " && ".join([
            'copy="$(mktemp -d)/package"',
            'cp -RL "$2" "$copy"',
            'chmod -R a-x,a+X,u+w "$copy"',
            '"$1" --dir "$copy" pack --out "$PWD/$3" > /dev/null',
        ]),
        arguments = [pnpm.executable.path, ctx.file.package.path, out.path],
        env = {"BAZEL_BINDIR": ctx.bin_dir.path},
        mnemonic = "PnpmPack",
        progress_message = "Packing %{label}",
    )
    return [DefaultInfo(files = depset([out]))]

# The tarball `pnpm pack` makes of an npm_package directory. Not npm_package's `packable`: in a
# sandbox, an action's inputs are symlinks, which `pnpm pack` leaves out, so it packs only
# package.json. This packs a copy with the links resolved.
_pnpm_pack = rule(
    implementation = _pnpm_pack_impl,
    attrs = {
        "package": attr.label(allow_single_file = True, mandatory = True),
        "_pnpm": attr.label(default = "@pnpm", executable = True, cfg = "exec"),
    },
)

def _published_npm_package_impl(name, visibility, srcs, replace_prefixes):
    package_json = [src for src in srcs if src.package == native.package_name() and src.name == "package.json"]
    if not package_json:
        fail("published_npm_package %s: srcs must include this package's package.json" % name)

    npm_package(
        name = name,
        visibility = visibility,
        srcs = srcs,
        replace_prefixes = replace_prefixes,
    )

    _pnpm_pack(
        name = name + "_tgz",
        package = ":" + name,
        visibility = visibility,
    )

    js_test(
        name = name + "_tarball_matches_package_json_test",
        data = [
            ":%s_tgz" % name,
            package_json[0],
        ],
        entry_point = Label(":npm_tarball_test"),
        args = [
            "$(rootpath :%s_tgz)" % name,
            "$(rootpath %s)" % package_json[0],
        ],
    )

published_npm_package = macro(
    doc = """A workspace package as npm publishes it: rules_js's `npm_package`, and `<name>.tgz`
    (`:<name>_tgz`), the tarball the release workflows publish.

    `<name>_tarball_matches_package_json_test` checks the tarball against package.json
    (bazel/npm_tarball_test.mjs): every path package.json names is in it, `files` covers all of
    it, its relative imports resolve inside it, and it ships no TypeScript sources or tests.
    """,
    implementation = _published_npm_package_impl,
    attrs = {
        "srcs": attr.label_list(mandatory = True, allow_files = True, configurable = False, doc = "What the package contains: package.json, README, LICENSE, built files."),
        "replace_prefixes": attr.string_dict(configurable = False, doc = "npm_package's: where built files land in the package, e.g. {\"src/\": \"dist/src/\"}."),
    },
)
