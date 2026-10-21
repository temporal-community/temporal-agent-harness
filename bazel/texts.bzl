"""harness_texts: text files both SDKs share, as a committed module of string constants."""

load("//bazel:committed_files.bzl", "committed_files")

def _texts_module_impl(ctx):
    # Not the committed name, which is the source file's in this package; the extension, which
    # picks the language, stays.
    ext = ctx.attr.module[ctx.attr.module.rfind("."):]
    out = ctx.actions.declare_file(ctx.label.name + ".generated" + ext)
    texts = sorted(ctx.files.srcs, key = lambda f: f.path)
    ctx.actions.run(
        inputs = texts,
        outputs = [out],
        executable = ctx.executable._writer,
        arguments = [ctx.attr.label, ctx.attr.update, out.path] + [f.path for f in texts],
        mnemonic = "HarnessTexts",
        progress_message = "Writing %{label}",
    )
    return [DefaultInfo(files = depset([out]))]

_texts_module = rule(
    implementation = _texts_module_impl,
    attrs = {
        "srcs": attr.label_list(mandatory = True, allow_files = [".txt"]),
        "module": attr.string(mandatory = True),
        "label": attr.string(mandatory = True),
        "update": attr.string(mandatory = True),
        "_writer": attr.label(default = "//bazel:texts_module", executable = True, cfg = "exec"),
    },
)

def _harness_texts_impl(name, visibility, srcs, out):
    module = out.name
    if not (module.endswith(".py") or module.endswith(".ts")):
        fail("harness_texts %s: out must be a .py or .ts file, not %s" % (name, module))
    _texts_module(
        name = name + "_module",
        srcs = srcs,
        module = module,
        label = "//%s:%s" % (native.package_name(), name),
        update = "//%s:%s_update" % (native.package_name(), name),
    )
    committed_files(
        name = name,
        srcs = [out],
        generated = {module: ":%s_module" % name},
        visibility = visibility,
    )

harness_texts = macro(
    doc = """Text files both SDKs share (model-facing prose), as a committed module of constants.

    Writes `out`, a Python (`.py`) or TypeScript (`.ts`) module with one string constant per text
    file, named after it in upper case. `bazel run :<name>_update` writes it, and the
    `committed_files` test fails while the committed copy differs.
    """,
    implementation = _harness_texts_impl,
    attrs = {
        "srcs": attr.label_list(mandatory = True, allow_files = [".txt"], configurable = False, doc = "The text files, usually a contract/ filegroup."),
        "out": attr.label(mandatory = True, allow_single_file = [".py", ".ts"], configurable = False, doc = "The committed module, e.g. `_texts.py` or `texts.ts`, in this package."),
    },
)
