# ABOUTME: `tests` is a package so that pytest resolves every test module from the REPO ROOT
# rather than from whichever subdirectory happens to be the first one without an __init__.py.
#
# Without this, `tests/state/` (a package, because its modules import each other relatively)
# made pytest put `tests/` itself on sys.path — and `tests/` contains `examples/`, which then
# shadowed the repo's real top-level `examples` package. The symptom was remote from the cause:
# the Monty workflow-sandbox re-import failed with `No module named 'examples.monty.workflow'`,
# but only in a full-suite run, and only once a test under tests/state had been collected first.
