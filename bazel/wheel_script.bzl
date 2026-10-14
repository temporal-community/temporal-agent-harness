"""wheel_script: one executable from a wheel's `<dist>.data/scripts/` directory.

rules_py builds a venv's bin/ from entry points only; executables a wheel ships under PEP 427's
`scripts` category (e.g. pydantic-monty-runtime's `monty`) are not linked into it.
"""

_TAR_TOOLCHAIN = "@tar.bzl//tar/toolchain:type"

def _wheel_script_impl(ctx):
    out = ctx.actions.declare_file(ctx.attr.script or ctx.label.name)
    tar = ctx.toolchains[_TAR_TOOLCHAIN]
    ctx.actions.run_shell(
        inputs = [ctx.file.whl],
        outputs = [out],
        tools = tar.default.files,
        env = tar.tarinfo.default_env,
        # Fails (empty output) when the wheel has no such script.
        command = '"$1" -xOf "$2" "*.data/scripts/$3" > "$4" && test -s "$4" && chmod +x "$4"',
        arguments = [tar.tarinfo.binary.path, ctx.file.whl.path, ctx.attr.script or ctx.label.name, out.path],
        mnemonic = "WheelScript",
        progress_message = "Extracting %{label} from " + ctx.file.whl.basename,
    )
    return [DefaultInfo(executable = out, files = depset([out]))]

wheel_script = rule(
    implementation = _wheel_script_impl,
    doc = "Extracts the executable `<dist>.data/scripts/<script>` from a wheel.",
    attrs = {
        "whl": attr.label(
            doc = "The wheel, e.g. @pypi//<package>:whl.",
            allow_single_file = [".whl"],
            mandatory = True,
        ),
        "script": attr.string(
            doc = "The script's file name in the wheel; defaults to the target name.",
        ),
    },
    executable = True,
    toolchains = [_TAR_TOOLCHAIN],
)
