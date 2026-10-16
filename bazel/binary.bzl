"""harness_module_binary: a Python program run as its module, the kind Gazelle emits for scripts."""

load("@aspect_rules_py//py:defs.bzl", "py_binary", "py_library")
load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("//bazel:package_collisions.bzl", "PACKAGE_COLLISIONS")

_MONTY = Label("//bazel:monty")

def _import_root(package, imports):
    """The directory module names start from: `package` joined with its import root, if any."""
    parts = package.split("/") if package else []
    for part in (imports[0].split("/") if imports else []):
        if part == "..":
            parts = parts[:-1]
        elif part not in ("", "."):
            parts.append(part)
    return "/".join(parts)

def _module_name(src, imports):
    path = src.package + "/" + src.name.removesuffix(".py")
    root = _import_root(src.package, imports)
    if root:
        path = path.removeprefix(root + "/")

    # `python -m pkg` runs pkg/__main__.py.
    return path.removesuffix("/__main__").replace("/", ".")

def _harness_module_binary_impl(name, visibility, srcs, main, module, deps, imports, data, env, args):
    if module and (srcs or main):
        fail("harness_module_binary: give either `module` (run a module from deps) or `srcs`, not both")
    if not module:
        if not srcs:
            fail("harness_module_binary needs `srcs` or `module`")
        if main == None:
            # Gazelle lists the package's __init__.py beside the program's own file.
            programs = [src for src in srcs if src.name != "__init__.py"]
            if len(programs) != 1:
                fail("harness_module_binary needs `main` to pick the module to run among %s" % srcs)
            main = programs[0]
        elif main not in srcs:
            fail("harness_module_binary: main %s is not one of srcs %s" % (main, srcs))
        module = _module_name(main, imports)

        # The program's code, for other binaries to depend on (e.g. a schema tool built on the CLI).
        py_library(
            name = name + "_lib",
            srcs = srcs,
            imports = imports,
            visibility = visibility,
            deps = deps,
        )
        deps = [":" + name + "_lib"]

    # `python -m <module>`, so the program runs as its module, in its package. Written into the
    # entry point rather than passed as an argument, so the binary also works as an action tool.
    write_file(
        name = name + "_main",
        out = name + "_main.py",
        content = [
            "import os",
            "import runpy",
            "",
            "# BUILD files name tools relative to the runfiles root, where `bazel run` starts.",
            "if os.environ.get('MONTY_BIN'):",
            "    os.environ['MONTY_BIN'] = os.path.abspath(os.environ['MONTY_BIN'])",
            "",
            "# Under `bazel run`, run from the workspace root, as the justfiles' `uv run` recipes do, so",
            "# repo-relative paths (.env.local's TEMPORAL_CONFIG_FILE, agents.toml) resolve.",
            "if os.environ.get('BUILD_WORKSPACE_DIRECTORY'):",
            "    os.chdir(os.environ['BUILD_WORKSPACE_DIRECTORY'])",
            "runpy.run_module(%r, run_name='__main__', alter_sys=True)" % module,
            "",
        ],
    )
    py_binary(
        name = name,
        visibility = visibility,
        srcs = [":" + name + "_main.py"],
        main = ":" + name + "_main.py",
        args = args,
        deps = deps,
        # Code Mode agents run their scripts in the monty binary.
        data = data + [_MONTY],
        env = env | {"MONTY_BIN": "$(rootpath %s)" % _MONTY},
        # Names the binary's venv `<name>.venv`; rules_py's default `_<name>.venv` breaks the
        # naming rule for targets a symbolic macro declares.
        expose_venv = True,
        package_collisions = PACKAGE_COLLISIONS,
    )

harness_module_binary = macro(
    doc = """Runs one Python module as `python -m <module>` would, with MONTY_BIN set.

    Under `bazel run` it runs from the workspace root, so relative paths resolve against the repo.

    Declares `<name>_lib` (a py_library of `srcs`) beside the binary.
    """,
    implementation = _harness_module_binary_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = [".py"], configurable = False, doc = "The program's sources."),
        "main": attr.label(allow_single_file = [".py"], configurable = False, doc = "The module to run among srcs; required when srcs holds more than one besides __init__.py."),
        "module": attr.string(configurable = False, doc = "Instead of srcs: a module, provided by deps, to run."),
        "deps": attr.label_list(configurable = False, doc = "Libraries the program imports."),
        "imports": attr.string_list(configurable = False, doc = "Import roots, relative to this package (Gazelle sets the python_root); module names start there."),
        "data": attr.label_list(allow_files = True, configurable = False, doc = "Files the program reads at runtime."),
        "env": attr.string_dict(configurable = False, doc = "Extra environment variables."),
        "args": attr.string_list(configurable = False, doc = "Arguments to the program."),
    },
)
