"""The bank's three DuckDB databases, and the locked-down connection every tool reads through.

DuckDB has no users, roles or GRANTs, so access control is the tool policy's job. The
connection is the second line: the three files are attached read-only, then external access
is switched off and the configuration locked, so even a query the policy wrongly allowed can't
``ATTACH`` another file, read one with ``read_csv`` / ``read_parquet``, ``COPY`` out, install or
load an extension, or ``SET`` any of this back.
"""

import os
from pathlib import Path

import duckdb

DATABASES = ("analytics", "risk", "customer_pii")
DEFAULT_DATABASE = "analytics"

# Relative to the repo root, where `bazel run` starts the example's programs.
_DEFAULT_DATA_DIR = Path("python/examples/governed_analytics/data")


def data_dir() -> Path:
    """Where the ``<database>.duckdb`` files live: ``$GOVERNED_ANALYTICS_DATA_DIR``, or the
    example's ``data/`` directory."""
    return Path(os.environ.get("GOVERNED_ANALYTICS_DATA_DIR", _DEFAULT_DATA_DIR))


def connect(directory: Path | None = None, *, optimize: bool = True) -> duckdb.DuckDBPyConnection:
    """An in-memory connection with the three databases attached read-only, ``analytics`` as
    the default, and external access off and locked.

    ``optimize=False`` turns the optimizer off, so a query's plan keeps every table it names:
    the optimizer may prune a scan it can prove empty, which would hide that table from the
    policy.
    """
    directory = directory or data_dir()
    connection = duckdb.connect(
        config={
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "allow_community_extensions": False,
        }
    )
    try:
        for name in DATABASES:
            path = directory / f"{name}.duckdb"
            if not path.exists():
                raise FileNotFoundError(
                    f"{path} does not exist; generate the data with `just data`"
                )
            literal = str(path.resolve()).replace("'", "''")
            connection.execute(f"ATTACH '{literal}' AS {name} (READ_ONLY)")
        connection.execute(f"USE {DEFAULT_DATABASE}")
        if not optimize:
            connection.execute("PRAGMA disable_optimizer")
        connection.execute("SET enable_external_access = false")
        connection.execute("SET lock_configuration = true")
    except BaseException:
        connection.close()
        raise
    return connection
