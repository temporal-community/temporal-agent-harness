"""The package_collisions policy for every venv this repo builds."""

# temporalio-typesafe ships temporalio/typesafe inside the temporalio package, so every venv that
# includes it has two wheels providing top-level `temporalio`, and rules_py's default ("warning")
# prints that on every analysis. temporalio's own __init__ extends its __path__, which makes the
# pair importable; "ignore" keeps the same resolution without the output, at the cost of not reporting
# collisions between other wheels either.
PACKAGE_COLLISIONS = "ignore"
