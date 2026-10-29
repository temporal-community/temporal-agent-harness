"""python_dists.py <project> <out>: builds a Python project's wheel and sdist into <out>.

<project> is the source tree python_dists stages (bazel/python_dists.bzl): pyproject.toml and
everything it packages. setuptools' build backend runs on a writable copy of it, since it writes
build/ and an .egg-info beside the sources, and runs in-process, so nothing is downloaded.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import setuptools.build_meta as build_meta


def main() -> None:
    project, out = (Path(arg).resolve() for arg in sys.argv[1:])
    out.mkdir(parents=True, exist_ok=True)
    # The archives record each file's mode and mtime. Bazel's outputs are read-only and
    # executable, so the copy gets ordinary modes, and one mtime for every file.
    epoch = int(os.environ["SOURCE_DATE_EPOCH"])
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "project"
        # Inputs are symlinks in the sandbox; the copy holds the files themselves.
        shutil.copytree(project, copy, symlinks=False)
        for path in [copy, *copy.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
            os.utime(path, (epoch, epoch))
        os.chdir(copy)
        # setuptools logs every file it copies; show that only when the build fails.
        log_path = Path(tmp) / "build.log"
        succeeded = False
        with log_path.open("w") as log:
            saved = os.dup(1), os.dup(2)
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            try:
                build_meta.build_wheel(str(out))
                build_meta.build_sdist(str(out))
                succeeded = True
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                os.dup2(saved[0], 1)
                os.dup2(saved[1], 2)
                if not succeeded:
                    sys.stderr.write(log_path.read_text())


if __name__ == "__main__":
    main()
