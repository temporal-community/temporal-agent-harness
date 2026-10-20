"""python_dists: a Python package's wheel and sdist, built by its own build backend (setuptools)."""

load("@aspect_rules_py//py:defs.bzl", "PyInfo")
load("@bazel_lib//lib:copy_to_directory.bzl", "copy_to_directory")

# 1980-01-01, the earliest time a zip archive can record.
_SOURCE_DATE_EPOCH = "315532800"

def _package_files_impl(ctx):
    # What the libraries reach under the package directory: their sources, and their runfiles for
    # data (built files such as a UI bundle). Nothing outside it ships, so neither does a
    # dependency's code nor the files of other first-party packages they import.
    prefix = ctx.attr.package_dir + "/"
    reached = depset(transitive = [
        depset(transitive = [lib[PyInfo].transitive_sources, lib[DefaultInfo].default_runfiles.files])
        for lib in ctx.attr.deps
    ])
    files = [f for f in reached.to_list() if f.owner.repo_name == "" and f.short_path.startswith(prefix)]
    return [DefaultInfo(files = depset(files))]

_package_files = rule(
    implementation = _package_files_impl,
    attrs = {
        "deps": attr.label_list(mandatory = True, providers = [PyInfo]),
        "package_dir": attr.string(mandatory = True),
    },
)

def _python_dists_rule_impl(ctx):
    out = ctx.actions.declare_directory(ctx.label.name)
    ctx.actions.run(
        inputs = [ctx.file.project],
        outputs = [out],
        executable = ctx.executable._builder,
        arguments = [ctx.file.project.path, out.path],
        env = {"SOURCE_DATE_EPOCH": _SOURCE_DATE_EPOCH},
        mnemonic = "PythonDists",
        progress_message = "Building the wheel and sdist of %{label}",
    )
    return [DefaultInfo(files = depset([out]))]

_python_dists_rule = rule(
    implementation = _python_dists_rule_impl,
    attrs = {
        "project": attr.label(allow_single_file = True, mandatory = True),
        "_builder": attr.label(default = "//bazel:python_dists", executable = True, cfg = "exec"),
    },
)

def _python_dists_impl(name, visibility, srcs, deps, package):
    _package_files(
        name = name + "_package_files",
        deps = deps,
        package_dir = "%s/%s" % (native.package_name(), package),
    )

    # The project as setuptools reads it: each file at its path relative to this package, or to
    # the workspace root for a file outside it (the root's README.md and LICENSE).
    copy_to_directory(
        name = name + "_project",
        srcs = srcs + [":%s_package_files" % name],
    )

    _python_dists_rule(
        name = name,
        project = ":%s_project" % name,
        visibility = visibility,
    )

python_dists = macro(
    doc = """`<name>/`, the directory holding a Python package's wheel and sdist, named as PyPI
    takes them.

    The package is what `deps` reach under `package` (a directory beside this BUILD file): their
    transitive sources and the data in their runfiles. setuptools' build backend builds both
    archives from that and `srcs` (bazel/python_dists.py), staged as a source tree,
    `<name>_project`, with no network: pyproject.toml supplies the metadata, as it does for
    `uv build`.
    """,
    implementation = _python_dists_impl,
    attrs = {
        "srcs": attr.label_list(mandatory = True, allow_files = True, configurable = False, doc = "pyproject.toml and the files it reads (README, license)."),
        "deps": attr.label_list(mandatory = True, configurable = False, doc = "The libraries that make up the package."),
        "package": attr.string(mandatory = True, configurable = False, doc = "The package's directory, relative to this BUILD file."),
    },
)
