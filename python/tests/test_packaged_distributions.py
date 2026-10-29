"""The wheel and sdist PyPI publishes (//python:dists): what ships in them, and that it runs."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

# //python:dists, relative to the runfiles root the test starts in.
DISTS = Path(os.environ["PYTHON_DISTS"]).resolve()
WHEEL_UI_PREFIX = "temporal_agent_harness/ui/dist/"


@pytest.fixture(scope="module")
def built_distributions() -> tuple[Path, Path]:
    (wheel,) = DISTS.glob("*.whl")
    (sdist,) = DISTS.glob("*.tar.gz")
    return wheel, sdist


def test_built_distributions_include_packaged_ui_assets(
    built_distributions: tuple[Path, Path],
) -> None:
    wheel_path, sdist_path = built_distributions

    with zipfile.ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
        _assert_ui_assets_present(names)
        assert "temporal_agent_harness/web/session_manager_worker.py" in names

        # The dist-info directory name embeds the package version, so match it rather than
        # hardcoding it — cutting a release shouldn't require editing this test.
        (metadata_name,) = [
            name
            for name in names
            if re.fullmatch(r"temporal_agent_harness-[^/]+\.dist-info/METADATA", name)
        ]
        metadata = wheel.read(metadata_name).decode()
        assert "Provides-Extra: ui" in metadata
        assert 'Requires-Dist: fastapi[standard]>=0.136.3; extra == "ui"' in metadata

        html = wheel.read(f"{WHEEL_UI_PREFIX}index.html").decode()
        for asset_path in _relative_asset_paths(html):
            assert f"{WHEEL_UI_PREFIX}{asset_path}" in names

    with tarfile.open(sdist_path) as sdist:
        names = set(sdist.getnames())
        assert any(name.endswith("/temporal_agent_harness/web/session_manager_worker.py") for name in names)
        assert any(name.endswith("/temporal_agent_harness/ui/dist/index.html") for name in names)
        assert any(name.endswith("/temporal_agent_harness/ui/dist/temporal-logo.svg") for name in names)
        assert any(
            name.endswith(".js") and "/temporal_agent_harness/ui/dist/assets/" in name
            for name in names
        )
        assert any(
            name.endswith(".css") and "/temporal_agent_harness/ui/dist/assets/" in name
            for name in names
        )


def test_built_distributions_expose_ui_server_console_script(
    built_distributions: tuple[Path, Path],
) -> None:
    """The prebuilt UI must be runnable straight from an install, not just from a checkout."""
    wheel_path, _ = built_distributions

    with zipfile.ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
        # The CLI is parsing only; the work it dispatches to must ship alongside it.
        for module in (
            "temporal_agent_harness/web/cli.py",
            "temporal_agent_harness/web/serve.py",
            "temporal_agent_harness/web/client.py",
            "temporal_agent_harness/web/session_manager_worker.py",
        ):
            assert module in names

        (entry_points_name,) = [
            name
            for name in names
            if re.fullmatch(r"temporal_agent_harness-[^/]+\.dist-info/entry_points.txt", name)
        ]
        entry_points = wheel.read(entry_points_name).decode()

    assert "[console_scripts]" in entry_points
    assert "temporal-agent-harness = temporal_agent_harness.web.cli:main" in entry_points


def test_built_distributions_exclude_nexus(
    built_distributions: tuple[Path, Path],
) -> None:
    """Neither the repo's top-level ``nexus/`` nor ``temporal_agent_harness/nexus_agent_adapter/``
    ships in the package.

    ``nexus/`` holds the separate Go/Nexus projects (``nexus/mcp`` is wired in only as a
    workspace-local editable source for the ``nexus-mcp`` extra). The adapter is not published:
    Bazel does not build it, so nothing in ``//python/temporal_agent_harness`` reaches it.
    """
    wheel_path, sdist_path = built_distributions

    with zipfile.ZipFile(wheel_path) as wheel:
        wheel_names = set(wheel.namelist())
    with tarfile.open(sdist_path) as sdist:
        # sdist members are prefixed with "<name>-<version>/"; strip it before checking.
        sdist_names = {name.split("/", 1)[-1] for name in sdist.getnames()}

    for names in (wheel_names, sdist_names):
        assert not [name for name in names if name.startswith("nexus/")]
        assert not [name for name in names if name.startswith("temporal_agent_harness/nexus_agent_adapter")]


def test_built_distributions_record_writable_non_executable_files(
    built_distributions: tuple[Path, Path],
) -> None:
    """Bazel's outputs are read-only and executable; the archives must not pass that on."""
    wheel_path, sdist_path = built_distributions

    with zipfile.ZipFile(wheel_path) as wheel:
        wheel_modes = {info.filename: info.external_attr >> 16 for info in wheel.infolist()}
    with tarfile.open(sdist_path) as sdist:
        sdist_modes = {member.name: member.mode for member in sdist if member.isfile()}

    for modes in (wheel_modes, sdist_modes):
        unusual = {name: oct(mode) for name, mode in modes.items() if mode & 0o111 or not mode & 0o200}
        assert unusual == {}


def test_extracted_wheel_can_resolve_packaged_ui_dist(
    built_distributions: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    wheel_path, _ = built_distributions
    extracted = tmp_path / "wheel"
    extracted.mkdir()

    with zipfile.ZipFile(wheel_path) as wheel:
        wheel.extractall(extracted)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(extracted)
    script = """
from temporal_agent_harness.ui import packaged_ui_dist

dist = packaged_ui_dist()
assert dist is not None
assert (dist / "index.html").is_file()
assert (dist / "temporal-logo.svg").is_file()
print(dist)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    # The package imported is the extracted wheel's, not the source tree's.
    assert Path(result.stdout.strip()).is_relative_to(extracted)


def test_every_module_in_the_wheel_imports_from_the_wheel_alone(
    built_distributions: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """The wheel holds every first-party module its modules import at load time.

    What ships is what //python/temporal_agent_harness reaches in Bazel's graph, so a module
    imported without a dependency edge (an import Gazelle cannot see, say) would be missing
    from it while every other test, importing from the source tree, still passed. Third-party
    packages come from this test's environment, which has every extra the package declares.
    """
    wheel_path, _ = built_distributions
    extracted = tmp_path / "wheel"
    with zipfile.ZipFile(wheel_path) as wheel:
        wheel.extractall(extracted)

    modules = sorted(
        ".".join(path.relative_to(extracted).with_suffix("").parts).removesuffix(".__init__")
        for path in (extracted / "temporal_agent_harness").rglob("*.py")
    )
    script = """
import importlib, json, sys, traceback
from pathlib import Path

extracted = Path(sys.argv[1])
failures = {}
for name in sys.argv[2:]:
    try:
        module = importlib.import_module(name)
    except BaseException:
        failures[name] = traceback.format_exc(limit=-3)
        continue
    if not Path(module.__file__).is_relative_to(extracted):
        failures[name] = f"loaded from {module.__file__}, not the wheel"
print(json.dumps(failures))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(extracted)
    result = subprocess.run(
        [sys.executable, "-c", script, str(extracted), *modules],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    failures = json.loads(result.stdout.splitlines()[-1])
    # Needs the `nexus-mcp` extra's distribution, which Bazel does not build (nexus/ is outside
    # it); the module says how to install it. Any other failure there still fails this test.
    nexus_mcp = failures.pop("temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp", "")
    assert "No module named 'nexus_mcp'" in nexus_mcp, nexus_mcp
    assert failures == {}, "\n\n".join(f"{name}:\n{error}" for name, error in failures.items())
    assert len(modules) > 50


def _relative_asset_paths(index_html: str) -> set[str]:
    return set(re.findall(r'(?:src|href)="\./([^"]+)"', index_html))


def _assert_ui_assets_present(names: set[str]) -> None:
    assert f"{WHEEL_UI_PREFIX}index.html" in names
    assert f"{WHEEL_UI_PREFIX}temporal-logo.svg" in names
    assert any(
        name.startswith(f"{WHEEL_UI_PREFIX}assets/") and name.endswith(".js")
        for name in names
    )
    assert any(
        name.startswith(f"{WHEEL_UI_PREFIX}assets/") and name.endswith(".css")
        for name in names
    )
