"""Run a local harness project from its harness.toml.

Starts Temporal when needed, the session manager, the web server, and configured workers.
File changes restart affected workers; registry changes reload the web server's agent list.
Ctrl-C stops all processes started by the runner.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import Manifest, WorkerSpec, load_manifest

_COLORS = ["36", "35", "33", "32", "34", "91", "96", "95", "93", "92", "94"]
_RESTART_GRACE = 6.0
_TEMPORAL_READY_TIMEOUT = 60.0


def run_dev(
    manifest_path: str | Path = "harness.toml",
    *,
    fresh: bool = False,
    watch: bool = True,
    only: Iterable[str] = (),
) -> None:
    manifest = load_manifest(manifest_path)
    runner = DevRunner(manifest, fresh=fresh, watch=watch, only=set(only))
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        pass


class Console:
    """Prefixed, optionally coloured lines on stdout, one per process."""

    def __init__(self) -> None:
        self._tty = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self._colors: dict[str, str] = {}
        self._width = 10

    def register(self, name: str) -> None:
        if name not in self._colors:
            self._colors[name] = _COLORS[len(self._colors) % len(_COLORS)]
            self._width = max(self._width, len(name))

    def line(self, name: str, text: str) -> None:
        label = f"{name:>{self._width}} │"
        if self._tty:
            label = f"\033[{self._colors.get(name, '0')}m{label}\033[0m"
        print(f"{label} {text}", flush=True)

    def note(self, text: str) -> None:
        self.line("dev", text)


@dataclass
class Proc:
    """One supervised child process, with its output prefixed by name."""

    name: str
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    console: Console
    watch: list[Path] = field(default_factory=list)
    _process: asyncio.subprocess.Process | None = None
    _reader: asyncio.Task[None] | None = None
    _stopping: bool = False

    async def start(self) -> None:
        self._stopping = False
        self.console.register(self.name)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=self.cwd,
                env=self.env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                # Its own process group, so stopping it also stops anything it spawned.
                start_new_session=True,
            )
        except FileNotFoundError:
            self.console.line(self.name, f"can't start: `{self.argv[0]}` not found")
            return
        self._reader = asyncio.create_task(self._pump(self._process))

    async def _pump(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            text = raw.decode(errors="replace").rstrip()
            self.console.line(self.name, text)
        code = await process.wait()
        if self._stopping or process is not self._process:
            return
        self.console.line(self.name, f"exited with code {code}")

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def stop(self) -> None:
        self._stopping = True
        process = self._process
        if process is None or process.returncode is not None:
            return
        _signal_group(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), _RESTART_GRACE)
        except TimeoutError:
            _signal_group(process.pid, signal.SIGKILL)
            await process.wait()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)

    async def restart(self, why: str) -> None:
        self.console.line(self.name, f"restarting ({why})")
        await self.stop()
        await self.start()


def _signal_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _temporal_target(env: dict[str, str], default_port: int) -> tuple[str, int]:
    """Where the project's Temporal is: TEMPORAL_ADDRESS, a temporal.toml profile, or local."""
    address = env.get("TEMPORAL_ADDRESS")
    if not address:
        try:
            from temporalio.envconfig import ClientConfig

            address = ClientConfig.load_client_connect_config(
                override_env_vars=env,
            ).get("target_host")
        except Exception:  # noqa: BLE001 - fall back to the local default
            address = None
    host, _, port = (address or f"localhost:{default_port}").rpartition(":")
    return host or "localhost", int(port or default_port)


class DevRunner:
    def __init__(
        self,
        manifest: Manifest,
        *,
        fresh: bool = False,
        watch: bool = True,
        only: set[str] | None = None,
        host: str = "127.0.0.1",
        port: int | None = None,
    ) -> None:
        self.manifest = manifest
        self.host = host
        self.port = port or manifest.dev.gateway_port
        self.fresh = fresh
        self.watch_enabled = watch
        self.only = only or set()
        self.console = Console()
        self.env = manifest.environment()
        self.env.setdefault("PYTHONUNBUFFERED", "1")
        self.procs: list[Proc] = []
        self.gateway: Proc | None = None
        self.workers: list[Proc] = []
        # Size the name column once, up front, so every line lines up from the first.
        for name in ("dev", "temporal", "manager", "gateway"):
            self.console.register(name)
        for worker in manifest.workers:
            self.console.register(worker.name)

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        watcher: asyncio.Task[None] | None = None
        try:
            self.console.note(f"project {self.manifest.project.name} ({self.manifest.path})")
            if not await self._start_temporal():
                return
            await self._start_platform()
            await self._start_workers()
            self._summary()
            if self.watch_enabled:
                watcher = asyncio.create_task(self._watch(stop))
            await stop.wait()
        finally:
            self.console.note("stopping…")
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            for proc in reversed(self.procs):
                await proc.stop()
            self.console.note("stopped")

    def _proc(
        self,
        name: str,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        watch: list[Path] | None = None,
    ) -> Proc:
        proc = Proc(
            name=name,
            argv=argv,
            cwd=cwd or self.manifest.root,
            env=env or self.env,
            console=self.console,
            watch=watch or [],
        )
        self.procs.append(proc)
        return proc

    async def _start_temporal(self) -> bool:
        dev = self.manifest.dev
        host, port = _temporal_target(self.env, dev.temporal_port)
        # Children must connect to the same server, including a custom [dev].temporal_port.
        self.env["TEMPORAL_ADDRESS"] = f"{host}:{port}"
        if _reachable(host, port):
            self.console.note(f"using the Temporal server already running at {host}:{port}")
            if self.fresh:
                self.console.note("--fresh ignored: this server wasn't started by dev")
            return True
        if host not in ("localhost", "127.0.0.1", "::1"):
            self.console.note(f"can't reach Temporal at {host}:{port}; start it, or unset its address")
            return False
        if shutil.which("temporal") is None:
            self.console.note("the `temporal` CLI isn't on PATH; install it to run a local dev server")
            return False
        state = self.manifest.resolve(dev.state_dir)
        state.mkdir(parents=True, exist_ok=True)
        (state / ".gitignore").write_text("*\n")
        db = state / "temporal.db"
        if self.fresh and db.exists():
            db.unlink()
            self.console.note("--fresh: starting Temporal with an empty database")
        proc = self._proc(
            "temporal",
            [
                "temporal", "server", "start-dev",
                "--db-filename", str(db),
                "--ip", "127.0.0.1" if host == "localhost" else host,
                "--port", str(port),
                "--ui-port", str(dev.temporal_ui_port),
                "--log-level", "warn",
            ],
        )
        await proc.start()
        deadline = asyncio.get_running_loop().time() + _TEMPORAL_READY_TIMEOUT
        while not _reachable(host, port):
            if not proc.running or asyncio.get_running_loop().time() > deadline:
                self.console.note("Temporal didn't come up; see its output above")
                return False
            await asyncio.sleep(0.25)
        # The frontend accepts connections a moment before the default namespace answers.
        await asyncio.sleep(1.5)
        return True

    async def _start_platform(self) -> None:
        cli = [sys.executable, "-m", "temporal_agent_harness.web.cli"]
        await self._proc("manager", [*cli, "session-manager"]).start()
        self.gateway = self._proc(
            "gateway",
            [
                *cli, "serve",
                "--manifest", str(self.manifest.path),
                "--host", self.host,
                "--port", str(self.port),
            ],
        )
        await self.gateway.start()

    async def _start_workers(self) -> None:
        for spec in self.manifest.workers:
            if self.only and spec.name not in self.only:
                continue
            worker_env = {**self.env, **spec.env}
            missing = self.manifest.missing_requirements(spec, worker_env)
            if missing:
                self.console.note(f"skipping worker {spec.name}: {', '.join(missing)} not set")
                continue
            proc = self._proc(
                spec.name,
                _worker_argv(spec),
                env=worker_env,
                watch=[self.manifest.resolve(p) for p in spec.watch],
            )
            self.workers.append(proc)
            await proc.start()

    def _summary(self) -> None:
        dev = self.manifest.dev
        self.console.note(f"console:     http://localhost:{self.port}")
        self.console.note(f"Temporal UI: http://localhost:{dev.temporal_ui_port}")
        self.console.note(
            f"{len(self.workers)} worker(s) running"
            + ("; watching for changes" if self.watch_enabled else "")
            + ". Ctrl-C stops everything."
        )

    # ------------------------------------------------------------------ watching

    def watch_roots(self) -> list[Path]:
        roots = {p for proc in self.workers for p in proc.watch if p.exists()}
        roots.add(self.manifest.path)
        roots.update(p for p in self.manifest.registry_paths() if p.exists())
        return sorted(roots)

    async def _watch(self, stop: asyncio.Event) -> None:
        try:
            from watchfiles import awatch
        except ModuleNotFoundError:
            self.console.note("file watching needs the 'ui' extra (watchfiles); not watching")
            return

        roots = self.watch_roots()
        async for changes in awatch(
            *roots, watch_filter=watch_filter(), debounce=600, stop_event=stop
        ):
            await self.on_changes({Path(path) for _change, path in changes})

    async def on_changes(self, changed: set[Path]) -> None:
        """Restart affected workers and refresh the web server when registries change."""
        registries = {self.manifest.path, *self.manifest.registry_paths()}
        if changed & registries and self.gateway is not None:
            await self.gateway.restart("agent registry changed")
            if self.manifest.path in changed:
                self.console.note(
                    "harness.toml changed: the agent list is reloaded; restart dev to pick up "
                    "changed [[workers]]"
                )
        for proc in self.workers:
            hits = [p for p in changed if any(_is_within(p, root) for root in proc.watch)]
            if hits:
                await proc.restart(_describe(hits, self.manifest.root))


def watch_filter():  # type: ignore[no-untyped-def]
    """Ignore build output and local state when watching worker source files."""
    from watchfiles import DefaultFilter

    class _Filter(DefaultFilter):
        ignore_dirs = (*DefaultFilter.ignore_dirs, "dist", ".workspaces", ".harness", ".vite")

    return _Filter()


def _worker_argv(spec: WorkerSpec) -> list[str]:
    # A leading `python` means "the interpreter dev runs under", so a worker gets the same
    # virtualenv (and dependency groups) as `uv run ... temporal-agent-harness dev`.
    head, *rest = spec.command
    return [sys.executable if head in ("python", "python3") else head, *rest]


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _describe(paths: list[Path], root: Path) -> str:
    first = paths[0]
    try:
        shown = first.relative_to(root)
    except ValueError:
        shown = first
    return f"{shown} changed" + (f" (+{len(paths) - 1} more)" if len(paths) > 1 else "")
