"""Runs the example programs a harness_dev target lists, for the examples named on the command line.

Every output line is prefixed with the program it came from, and Ctrl-C (or SIGTERM) reaches every
program. A program that exits on its own is reported; the others keep running.

    dev_runner <instructions.json> [example ...]

An example is named by its directory under examples/ (`callback_tools/wiki_agent`) or that path's
last segment (`wiki_agent`); naming none runs every example.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TextIO

# Seconds a program gets to stop after the forwarded signal, then after SIGTERM, before SIGKILL.
_GRACE = 10.0
_COLORS = ("36", "33", "35", "32", "34", "31", "96", "93", "95", "92", "94", "91")


@dataclass(frozen=True)
class Program:
    """One executable in the runfiles tree, with the environment `bazel run` would give it."""

    path: Path
    env: dict[str, str]

    @staticmethod
    def from_json(blob: dict[str, object]) -> Program:
        env = blob["env"]
        assert isinstance(env, dict)
        return Program(path=Path(str(blob["path"])).resolve(), env={str(k): str(v) for k, v in env.items()})


@dataclass(frozen=True)
class Launch:
    """A program to start, its arguments, and the label its output lines carry."""

    label: str
    program: Program
    args: tuple[str, ...]


def _choose(examples: list[str], names: list[str]) -> list[str]:
    if not names:
        return examples
    chosen: list[str] = []
    for name in names:
        name = name.rstrip("/")
        matches = [e for e in examples if e == name or e.endswith("/" + name)]
        if len(matches) != 1:
            choices = "\n".join("  " + e for e in examples)
            sys.exit(f"error: '{name}' does not name one example. Choose from:\n{choices}")
        if matches[0] not in chosen:
            chosen.append(matches[0])
    return chosen


def _launches(instructions: dict[str, object], names: list[str]) -> list[Launch]:
    examples = instructions["examples"]
    assert isinstance(examples, list)
    chosen = _choose([str(e) for e in examples], names)
    registries = [f"examples/{example}/agents.toml" for example in chosen]

    launches: list[Launch] = []
    cli_blob = instructions.get("cli")
    if isinstance(cli_blob, dict):
        cli = Program.from_json(cli_blob)
        if instructions["session_manager"]:
            launches.append(Launch("session-manager", cli, ("session-manager",)))
        if instructions["server"]:
            serve = ("serve", *registries, "--host", "0.0.0.0", "--port", "8000")
            launches.append(Launch("server", cli, serve))
    workers = instructions["workers"]
    assert isinstance(workers, dict)
    for example in chosen:
        if example in workers:
            launches.append(Launch(example.rsplit("/", 1)[-1], Program.from_json(workers[example]), ()))
    return launches


class _Output:
    """Writes whole lines from every program to stdout, each behind its program's label."""

    def __init__(self, stream: TextIO, labels: list[str]) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._width = max(map(len, labels), default=0)
        self._color = stream.isatty() and "NO_COLOR" not in os.environ
        self._colors = {label: _COLORS[i % len(_COLORS)] for i, label in enumerate(labels)}

    def line(self, label: str, text: str) -> None:
        prefix = f"{label:<{self._width}} │ "
        if self._color:
            prefix = f"\033[{self._colors[label]}m{prefix}\033[0m"
        with self._lock:
            self._stream.write(prefix + text.rstrip("\n") + "\n")
            self._stream.flush()


def _pump(launch: Launch, process: subprocess.Popen[str], output: _Output, stopping: threading.Event) -> None:
    assert process.stdout is not None
    for text in process.stdout:
        output.line(launch.label, text)
    code = process.wait()
    # A negative code is the signal that ended the program, which stopping sends.
    output.line(launch.label, "stopped" if stopping.is_set() and code <= 0 else f"exited with code {code}")


def main(argv: list[str]) -> int:
    instructions = json.loads(Path(argv[1]).read_text())
    launches = _launches(instructions, argv[2:])
    output = _Output(sys.stdout, [launch.label for launch in launches])

    processes: list[subprocess.Popen[str]] = []
    stopping = threading.Event()

    def stop(signum: int, _frame: FrameType | None) -> None:
        if stopping.is_set():
            return
        stopping.set()
        for process in processes:
            if process.poll() is None:
                process.send_signal(signum)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    pumps: list[threading.Thread] = []
    for launch in launches:
        process = subprocess.Popen(
            [str(launch.program.path), *launch.args],
            env={**os.environ, **launch.program.env, "PYTHONUNBUFFERED": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        processes.append(process)
        pump = threading.Thread(target=_pump, args=(launch, process, output, stopping), daemon=True)
        pump.start()
        pumps.append(pump)

    while any(process.poll() is None for process in processes) and not stopping.wait(timeout=0.5):
        pass
    # Stopping: escalate on any program that outlasts the grace period after each signal.
    if stopping.is_set():
        for escalate in (signal.SIGTERM, signal.SIGKILL):
            try:
                for process in processes:
                    process.wait(timeout=_GRACE)
                break
            except subprocess.TimeoutExpired:
                for process in processes:
                    if process.poll() is None:
                        process.send_signal(escalate)
    for process in processes:
        process.wait()
    for pump in pumps:
        pump.join()
    return 0 if stopping.is_set() or all(p.returncode == 0 for p in processes) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
