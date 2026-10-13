"""Exercise shutdown with real process groups, signals, and surviving descendants."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from temporal_agent_harness.dev import runner as runner_module
from temporal_agent_harness.dev.runner import Console, Proc

from .test_runner import _runner, _until


@pytest.mark.parametrize("sig", [0, signal.SIGTERM, signal.SIGKILL])
@pytest.mark.parametrize("members", ["", "123 Z\n123 Z+\n456 S\n"])
def test_macos_zombie_only_group_is_already_stopped(monkeypatch, sig, members):
    monkeypatch.setattr(sys, "platform", "darwin")

    def denied(*args):
        raise PermissionError("group has no signalable members")

    monkeypatch.setattr(os, "killpg", denied)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, members)
    )
    if sig == 0:
        assert not runner_module._group_exists(123)
    else:
        runner_module._signal_group(123, sig)


@pytest.mark.parametrize("sig", [0, signal.SIGTERM, signal.SIGKILL])
@pytest.mark.parametrize("inspection", [
    "123 Z\n123 S\n",  # The leader exited, but a descendant is still alive.
    "invalid output",
    subprocess.CalledProcessError(1, "ps"),
    subprocess.TimeoutExpired("ps", 1),
    FileNotFoundError("ps"),
])
def test_macos_permission_errors_survive_live_or_unknown_group_state(monkeypatch, sig, inspection):
    monkeypatch.setattr(sys, "platform", "darwin")
    error = PermissionError("cannot signal live group")

    def denied(*args):
        raise error

    def inspect(*args, **kwargs):
        if isinstance(inspection, Exception):
            raise inspection
        return subprocess.CompletedProcess(args, 0, inspection)

    monkeypatch.setattr(os, "killpg", denied)
    monkeypatch.setattr(subprocess, "run", inspect)
    with pytest.raises(PermissionError) as exc:
        if sig == 0:
            runner_module._group_exists(123)
        else:
            runner_module._signal_group(123, sig)
    assert exc.value is error


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin's zombie-only group behavior")
async def test_real_macos_zombie_group_does_not_fail_cleanup():
    # Keep the exited child unreaped so the group remains present with only a zombie.
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    try:
        await _until(lambda: not _alive(process.pid))
        assert not runner_module._group_exists(process.pid)
        runner_module._signal_group(process.pid, signal.SIGTERM)
        runner_module._signal_group(process.pid, signal.SIGKILL)
    finally:
        process.wait(timeout=5)


def _alive(pid: int) -> bool:
    # Orphans may briefly remain as zombies until init reaps them, especially in CI.
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    )
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@pytest.mark.parametrize("parent_exits", [False, True])
@pytest.mark.parametrize("inherit_output", [False, True])
async def test_stop_kills_stubborn_descendants_even_after_parent_exit(
    tmp_path: Path, monkeypatch, parent_exits: bool, inherit_output: bool
):
    monkeypatch.setattr(runner_module, "_RESTART_GRACE", 0.2)
    ready = tmp_path / "child.pid"
    child_code = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        f"stdout={'None' if inherit_output else 'subprocess.DEVNULL'}, "
        "stderr=subprocess.DEVNULL); "
        + ("" if parent_exits else "time.sleep(60)")
    )
    proc = Proc("parent", [sys.executable, "-c", parent_code], tmp_path, dict(os.environ), Console())
    await proc.start()
    process = proc._process
    assert process is not None
    try:
        await _until(lambda: ready.exists() and bool(ready.read_text()))
        child_pid = int(ready.read_text())
        if parent_exits:
            await _until(lambda: process.returncode is not None)
        assert _alive(child_pid)
        await asyncio.wait_for(proc.stop(), 3)
        await _until(lambda: not _alive(child_pid))
        assert process.returncode is not None
        # Repeated cleanup must not signal a stale process-group ID.
        await proc.stop()
        assert proc._process is None
    finally:
        _kill_group(process.pid)
        await proc.stop()


async def test_cancellation_during_spawn_keeps_child_available_for_cleanup(tmp_path: Path, monkeypatch):
    spawned = asyncio.Event()
    release = asyncio.Event()
    original = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    proc = Proc(
        "worker", [sys.executable, "-c", "import time; time.sleep(60)"],
        tmp_path, dict(os.environ), Console(),
    )
    start = asyncio.create_task(proc.start())
    try:
        await asyncio.wait_for(spawned.wait(), 5)
        start.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await start
        assert proc._process is not None
        pid = proc._process.pid
        await proc.stop()
        assert not _alive(pid)
    finally:
        release.set()
        await asyncio.gather(start, return_exceptions=True)
        await proc.stop()


async def test_stop_allows_graceful_cleanup(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner_module, "_RESTART_GRACE", 2)
    ready, cleaned = tmp_path / "ready", tmp_path / "cleaned"
    code = f'''
import signal, time
from pathlib import Path
def shutdown(*_):
    time.sleep(0.1)
    Path({str(cleaned)!r}).touch()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, shutdown)
Path({str(ready)!r}).touch()
time.sleep(60)
'''
    proc = Proc("worker", [sys.executable, "-c", code], tmp_path, dict(os.environ), Console())
    await proc.start()
    process = proc._process
    assert process is not None
    try:
        await _until(ready.exists)
        await asyncio.wait_for(proc.stop(), 3)
        assert cleaned.exists()
        assert process.returncode == 0
    finally:
        _kill_group(process.pid)
        await proc.stop()


async def test_detached_descendant_cannot_hold_shutdown_open_via_stdout(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner_module, "_DRAIN_GRACE", 0.2)
    ready = tmp_path / "detached.pid"
    child_code = (
        "import os, time; from pathlib import Path; "
        f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    code = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], start_new_session=True)"
    )
    proc = Proc("parent", [sys.executable, "-c", code], tmp_path, dict(os.environ), Console())
    await proc.start()
    process = proc._process
    assert process is not None
    try:
        await _until(lambda: ready.exists() and bool(ready.read_text()))
        await _until(lambda: process.returncode is not None)
        await asyncio.wait_for(proc.stop(), 2)
        # Deliberate session escape is outside the runner's ownership boundary.
        assert _alive(int(ready.read_text()))
    finally:
        if ready.exists() and ready.read_text():
            _kill_group(int(ready.read_text()))
        _kill_group(process.pid)
        await proc.stop()
        assert process.stdout is not None
        await _until(process.stdout.at_eof)


async def test_cleanup_is_concurrent_and_attempts_every_service_on_error(tmp_path: Path):
    runner = _runner(tmp_path)
    entered: set[str] = set()
    all_entered = asyncio.Event()

    class Service:
        def __init__(self, name):
            self.name = name

        async def stop(self):
            entered.add(self.name)
            if len(entered) == 3:
                all_entered.set()
            await asyncio.wait_for(all_entered.wait(), 2)
            if self.name == "broken":
                raise RuntimeError("cleanup failure")

    async def start():
        runner.procs = [Service(name) for name in ("first", "broken", "last")]
        return False

    runner._start = start
    original_handler = signal.getsignal(signal.SIGINT)
    with pytest.raises(ExceptionGroup, match="dev process cleanup failed") as exc:
        await runner.run()
    assert entered == {"first", "broken", "last"}
    assert str(exc.value.exceptions[0]) == "cleanup failure"
    assert signal.getsignal(signal.SIGINT) == original_handler


# Run the supervisor in a separate interpreter so SIGINT exercises its real signal
# handlers without changing pytest's. Readiness files avoid timing-based signal races.
_DRIVER = '''
import asyncio
import os
import signal
import sys
from pathlib import Path
from temporal_agent_harness.dev import runner as module
from temporal_agent_harness.dev.manifest import Manifest

root = Path(sys.argv[1])
mode = sys.argv[2]
module._RESTART_GRACE = 0.2

class Runner(module.DevRunner):
    async def _start_temporal(self):
        child = "import time; time.sleep(60)"
        if mode == "reload-stop":
            child = (
                "import signal, time; from pathlib import Path; "
                f"signal.signal(signal.SIGTERM, lambda *_: Path({str(root / 'ready')!r}).touch()); "
                f"Path({str(root / 'child-ready')!r}).touch(); time.sleep(60)"
            )
        proc = self._proc("worker", [sys.executable, "-c", child])
        await proc.start()
        (root / "first.pid").write_text(str(proc._process.pid))
        self.worker = proc
        if mode == "reload-stop":
            while not (root / "child-ready").exists():
                await asyncio.sleep(0.01)
        if mode == "startup":
            (root / "ready").touch()
            await asyncio.Event().wait()
        return True

    async def _start_platform(self):
        pass

    async def _start_workers(self):
        pass

    async def _watch(self, stop):
        if mode == "reload-stop":
            await self.worker.restart("test")
        elif mode == "reload":
            original = asyncio.create_subprocess_exec
            async def delayed_spawn(*args, **kwargs):
                process = await original(*args, **kwargs)
                (root / "second.pid").write_text(str(process.pid))
                (root / "ready").touch()
                # Hold the spawn until cancellation of the watcher has arrived.
                while not stop.is_set():
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
                return process
            asyncio.create_subprocess_exec = delayed_spawn
            await self.worker.restart("test")
        else:
            (root / "ready").touch()
        await stop.wait()

asyncio.run(Runner(Manifest(path=root / "harness.toml")).run())
'''


@pytest.mark.parametrize("mode", ["startup", "running", "reload", "reload-stop"])
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("wrapper", [False, True], ids=["python", "just-uv"])
async def test_one_signal_stops_runner_and_its_children(
    tmp_path: Path, mode: str, sig: signal.Signals, wrapper: bool
):
    command = [sys.executable, "-c", _DRIVER, str(tmp_path), mode]
    if wrapper:
        if not shutil.which("just") or not shutil.which("uv"):
            pytest.skip("just/uv not installed")
        script = tmp_path / "driver.py"
        script.write_text(_DRIVER)
        justfile = tmp_path / "justfile"
        # Match the production just -> uv -> Python chain without starting the user's
        # configured workers or downloading dependencies in a regression test.
        justfile.write_text(
            "dev:\n    " + shlex.join(["uv", "run", "--no-sync", "python", str(script), str(tmp_path), mode]) + "\n"
        )
        command = ["just", "--justfile", str(justfile), "--working-directory", str(Path.cwd()), "dev"]
    driver = await asyncio.create_subprocess_exec(
        *command,
        start_new_session=True, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output = asyncio.create_task(driver.communicate())
    try:
        await _until(lambda: (tmp_path / "ready").exists())
        pids = [int(path.read_text()) for path in tmp_path.glob("*.pid")]
        assert any(_alive(pid) for pid in pids)
        os.killpg(driver.pid, sig)
        stdout, _ = await asyncio.wait_for(asyncio.shield(output), 5)
        # just itself can report interruption even though the runner exits cleanly.
        if not wrapper:
            assert driver.returncode == 0, stdout.decode()
        assert "stopped" in stdout.decode()
        assert all(not _alive(pid) for pid in pids)
    finally:
        for path in tmp_path.glob("*.pid"):
            _kill_group(int(path.read_text()))
        if driver.returncode is None:
            _kill_group(driver.pid)
        await asyncio.wait_for(output, 5)
