"""The dev runner's supervision: restarting what watches a change, and nothing else."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from temporal_agent_harness.dev.manifest import load_manifest
from temporal_agent_harness.dev.runner import Console, DevRunner, Proc, _worker_argv

MANIFEST = """
[project]
name = "demo"

[[workers]]
name = "a"
command = ["python", "-c", "import time; time.sleep(60)"]
watch = ["a"]

[[workers]]
name = "b"
command = ["python", "-c", "import time; time.sleep(60)"]
watch = ["b"]
"""


def _runner(tmp_path: Path) -> DevRunner:
    for d in ("a", "b"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "harness.toml").write_text(MANIFEST)
    return DevRunner(load_manifest(tmp_path / "harness.toml"))


class _Recorder:
    def __init__(self, name: str, watch: list[Path]) -> None:
        self.name = name
        self.watch = watch
        self.restarts: list[str] = []

    async def restart(self, why: str) -> None:
        self.restarts.append(why)


async def test_a_change_restarts_only_the_workers_that_watch_it(tmp_path: Path):
    runner = _runner(tmp_path)
    a = _Recorder("a", [tmp_path / "a"])
    b = _Recorder("b", [tmp_path / "b"])
    runner.workers = [a, b]  # type: ignore[list-item]
    await runner.on_changes({tmp_path / "b" / "worker.py"})
    assert (a.restarts, len(b.restarts)) == ([], 1)
    assert "b/worker.py changed" in b.restarts[0]


async def test_a_manifest_change_restarts_the_gateway(tmp_path: Path):
    runner = _runner(tmp_path)
    gateway = _Recorder("gateway", [])
    runner.gateway = gateway  # type: ignore[assignment]
    await runner.on_changes({runner.manifest.path})
    assert gateway.restarts == ["agent registry changed"]


def test_python_workers_run_under_devs_own_interpreter():
    from temporal_agent_harness.dev.manifest import WorkerSpec

    assert _worker_argv(WorkerSpec(name="w", command=["python", "-m", "x"]))[0] == sys.executable
    assert _worker_argv(WorkerSpec(name="w", command=["node", "x.js"]))[0] == "node"


async def test_a_process_restarts_as_a_new_process_and_stops_cleanly(tmp_path: Path):
    lines: list[str] = []

    class _Capture(Console):
        def line(self, name: str, text: str) -> None:
            lines.append(f"{name}: {text}")

    proc = Proc(
        name="sleeper",
        argv=[sys.executable, "-c", "import os, time; print(os.getpid(), flush=True); time.sleep(60)"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        console=_Capture(),
    )
    try:
        await proc.start()
        await _until(lambda: any(l.split(": ")[1].isdigit() for l in lines))
        first = proc._process.pid  # type: ignore[union-attr]
        await proc.restart("test")
        await _until(lambda: sum(1 for l in lines if l.split(": ")[1].isdigit()) == 2)
        assert proc._process.pid != first  # type: ignore[union-attr]
    finally:
        await proc.stop()
    assert not proc.running
    assert not any("exited with code" in l for l in lines), "a deliberate stop is not reported as an exit"


async def _until(check, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.05)


async def test_custom_temporal_port_reaches_every_child(tmp_path: Path, monkeypatch):
    import temporal_agent_harness.dev.runner as runner_module

    runner = _runner(tmp_path)
    runner.manifest.dev.temporal_port = 17233
    runner.env = {"TEMPORAL_CONFIG_FILE": str(tmp_path / "absent.toml")}
    targets = []

    def reachable(host, port):
        targets.append((host, port))
        return True

    async def start(self):
        pass

    monkeypatch.setattr(runner_module, "_reachable", reachable)
    monkeypatch.setattr(Proc, "start", start)
    assert await runner._start_temporal()
    await runner._start_platform()
    await runner._start_workers()
    assert targets == [("localhost", 17233)]
    assert all(p.env["TEMPORAL_ADDRESS"] == "localhost:17233" for p in runner.procs)


@pytest.mark.parametrize("only, expected", [(set(), ["b"]), ({"a"}, [])])
async def test_worker_selection_and_requirements(tmp_path: Path, monkeypatch, only, expected):
    runner = _runner(tmp_path)
    runner.only = only
    runner.env = {}
    runner.manifest.workers[0].requires = ["MISSING_KEY"]
    runner.manifest.workers[1].requires = ["WORKER_KEY"]
    runner.manifest.workers[1].env = {"WORKER_KEY": "configured"}
    started = []

    async def start(self):
        started.append(self.name)

    monkeypatch.setattr(Proc, "start", start)
    await runner._start_workers()
    assert started == expected


async def test_file_watcher_restarts_a_worker_after_saving(tmp_path: Path):
    runner = _runner(tmp_path)
    await runner._start_workers()
    a, b = runner.workers
    assert a._process is not None and b._process is not None
    old_a, old_b = a._process.pid, b._process.pid
    stop = asyncio.Event()
    watcher = asyncio.create_task(runner._watch(stop))
    try:
        # Allow the native watcher to subscribe before writing the worker source.
        await asyncio.sleep(0.5)
        (tmp_path / "a" / "worker.py").write_text("# saved\n")
        await _until(lambda: a._process is not None and a._process.pid != old_a)
        assert b._process.pid == old_b
    finally:
        stop.set()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await asyncio.gather(*(p.stop() for p in runner.procs))
    assert not any(p.running for p in runner.procs)


def test_build_output_and_local_state_do_not_trigger_reloads():
    from watchfiles import Change

    from temporal_agent_harness.dev.runner import watch_filter

    check = watch_filter()
    for relative in ("dist/index.js", ".harness/temporal.db", "__pycache__/worker.pyc"):
        assert not check(Change.modified, f"/repo/{relative}")
    assert check(Change.modified, "/repo/example/worker.py")
