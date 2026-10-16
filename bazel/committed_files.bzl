"""committed_files: generated files kept in the source tree, written and checked by Bazel."""

load("@bazel_lib//lib:copy_to_bin.bzl", "copy_to_bin")
load("@bazel_lib//lib:diff_test.bzl", "diff_test")
load("@bazel_lib//lib:write_source_files.bzl", "write_source_files")

def _test_name(name, path):
    # `<name>_<file>_up_to_date_test`: the file's name, without a leading `<name>.`, in snake case.
    file = path.rpartition("/")[2].removeprefix(name + ".")
    return "%s_%s_up_to_date_test" % (name, file.replace(".", "_").replace("-", "_"))

def _committed_files_impl(name, visibility, srcs, generated):
    committed = {src.name: src for src in srcs}
    if sorted(committed) != sorted(generated):
        fail("committed_files %s: srcs must be the committed %s" % (name, sorted(generated)))

    update = name + "_update"
    write_source_files(
        name = update,
        files = generated,
        # The named tests below check the files: write_source_files would name them after this
        # target, and its existence check globs, which a symbolic macro cannot.
        check_that_out_file_exists = False,
        diff_test = False,
    )

    # In the output tree too, where js_binary and js_test read their data.
    copy_to_bin(
        name = name + "_files",
        srcs = srcs,
        visibility = visibility,
    )
    for path in sorted(generated):
        diff_test(
            name = _test_name(name, path),
            failure_message = "%s is out of date. To update it, run:\n\n    bazel run //%s:%s\n" % (
                path,
                native.package_name(),
                update,
            ),
            file1 = generated[path],
            file2 = committed[path],
        )

committed_files = macro(
    doc = """Files that targets generate and the source tree keeps, e.g. for readers without Bazel.

    A building block for the macros that commit what they generate (harness_agent_registry,
    harness_types, harness_client_sdk); BUILD files use those instead.

    `bazel run :<name>_update` writes them; `<name>_<file>_up_to_date_test` fails while a committed
    copy differs from what its target generates. `:<name>_files` is the committed copies.
    """,
    implementation = _committed_files_impl,
    attrs = {
        "srcs": attr.label_list(
            mandatory = True,
            allow_files = True,
            configurable = False,
            doc = "The committed copies, one per `generated` path. Listing one before its first update is fine.",
        ),
        "generated": attr.string_keyed_label_dict(
            mandatory = True,
            allow_files = True,
            configurable = False,
            doc = "Each committed file's path in this package, and the target that generates it.",
        ),
    },
)
