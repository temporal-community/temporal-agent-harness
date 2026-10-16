"""harness_pytest_test: this repo's pytest test, the kind Gazelle emits for every test module."""

load("@aspect_rules_py//py:defs.bzl", "py_pytest_test")
load("//bazel:package_collisions.bzl", "PACKAGE_COLLISIONS")

_PYTEST = Label("@pypi//pytest")

# Plugins pyproject.toml's pytest config relies on (asyncio_mode), whatever a module imports.
_PYTEST_PLUGINS = [Label("@pypi//pytest_asyncio")]
_TEMPORAL_CLI = Label("//bazel/temporal:cli")
_TEMPORAL_TEST_SERVER = Label("//bazel/temporal:test_server")
_MONTY = Label("//bazel:monty")

def _harness_pytest_test_impl(name, visibility, srcs, deps, data, env, pytest_args, size, timeout, tags, flaky):
    py_pytest_test(
        name = name,
        visibility = visibility,
        srcs = srcs,
        deps = deps + [d for d in [_PYTEST] + _PYTEST_PLUGINS if d not in deps],
        # The repo's pytest settings (asyncio_mode, pythonpath, ...) apply as under `uv run pytest`.
        # Passed at run time, so every test shares rules_py's prebuilt pytest entry point.
        # consider_namespace_packages: a test's package __init__.py files belong to other Bazel
        # packages and are not in its runfiles, so pytest names modules from the import path
        # (tests.state.test_decl) and package-relative imports resolve.
        args = [
            "-c",
            "pyproject.toml",
            "--rootdir",
            ".",
            "-o",
            "consider_namespace_packages=true",
        ] + pytest_args,
        data = data + [
            Label("//:pyproject.toml"),
            _TEMPORAL_CLI,
            _TEMPORAL_TEST_SERVER,
            _MONTY,
        ],
        # Pinned tools, relative to the runfiles root (tests/conftest.py makes them absolute).
        env = env | {
            "MONTY_BIN": "$(rootpath %s)" % _MONTY,
            "TEMPORAL_CLI_PATH": "$(rootpath %s)" % _TEMPORAL_CLI,
            "TEMPORAL_TEST_SERVER_PATH": "$(rootpath %s)" % _TEMPORAL_TEST_SERVER,
        },
        size = size,
        timeout = timeout or None,
        tags = tags,
        flaky = flaky,
        # Names the test's venv `<name>.venv`; rules_py's default `_<name>.venv` breaks the naming
        # rule for targets a symbolic macro declares.
        expose_venv = True,
        package_collisions = PACKAGE_COLLISIONS,
    )

harness_pytest_test = macro(
    doc = """A pytest test module run with the repo's pyproject.toml config.

    It gets the Temporal servers pinned in MODULE.bazel (TEMPORAL_CLI_PATH,
    TEMPORAL_TEST_SERVER_PATH) and the Code Mode sandbox binary (MONTY_BIN).
    """,
    implementation = _harness_pytest_test_impl,
    attrs = {
        "srcs": attr.label_list(mandatory = True, allow_files = [".py"], configurable = False, doc = "The test modules pytest collects."),
        "deps": attr.label_list(configurable = False, doc = "Libraries the modules import; @pypi//pytest is added."),
        "data": attr.label_list(allow_files = True, configurable = False, doc = "Files the tests read at runtime."),
        "env": attr.string_dict(configurable = False, doc = "Extra environment variables."),
        "pytest_args": attr.string_list(configurable = False, doc = "Extra pytest arguments, e.g. --deselect=<node id>."),
        "size": attr.string(configurable = False, default = "medium", values = ["small", "medium", "large", "enormous"]),
        "timeout": attr.string(configurable = False, values = ["", "short", "moderate", "long", "eternal"]),
        "tags": attr.string_list(configurable = False),
        "flaky": attr.bool(configurable = False),
    },
)
