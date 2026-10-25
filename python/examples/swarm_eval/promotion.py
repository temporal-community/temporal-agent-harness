"""Prepare a source-pinned promotion patch without modifying the checkout or publishing anything."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from pathlib import Path
import subprocess

from examples.swarm_eval.incubation import (
    EvaluationArtifact,
    FitAssessment,
    ReadyDeclaration,
    SourceSnapshot,
    validate_declaration,
)


def repository_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, check=True, capture_output=True,
    )
    return sorted(set(p for p in result.stdout.decode().split("\0") if p))


def snapshot(root: Path) -> SourceSnapshot:
    """Record the actual checkout, including uncommitted sources; exclude gitignored local output."""
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    files = {}
    for name in repository_files(root):
        path = root / name
        if path.is_symlink():
            data = os.readlink(path).encode()
        elif path.is_file():
            data = path.read_bytes()
        else:
            continue  # Deleted tracked files are absent from this snapshot.
        files[name] = hashlib.sha256(data).hexdigest()
    return SourceSnapshot(commit=commit, files=files)


def _diff(name: str, before: str | None, after: str | None, *, executable: bool = False) -> str:
    if before == after:
        return ""
    header = f"diff --git a/{name} b/{name}\n"
    mode = "100755" if executable else "100644"
    if before is None:
        header += f"new file mode {mode}\n"
    if after is None:
        header += f"deleted file mode {mode}\n"
    lines = difflib.unified_diff(
        (before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True),
        fromfile=f"a/{name}" if before is not None else "/dev/null",
        tofile=f"b/{name}" if after is not None else "/dev/null",
    )
    # Git requires an explicit marker for a final line without a newline.
    body = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
    return header + body


def prepare_promotion(root: Path, artifact: EvaluationArtifact, fit: FitAssessment, declaration: ReadyDeclaration) -> str:
    """Move the Python example into a product, preserving its import namespace for callers.

    The product owns an import root containing examples/<module>. This avoids rewriting Python
    imports or breaking saved workflow definitions; physical ownership moves to /products.
    """
    validate_declaration(artifact, fit, declaration)
    if snapshot(root) != artifact.source:
        raise ValueError("checkout changed since evaluation; evaluate this revision before promotion")
    candidate = artifact.candidate
    source = f"python/examples/{candidate.example}"
    product = f"products/{candidate.slug}"
    destination = f"{product}/examples/{candidate.example}"
    if (root / product).exists():
        raise ValueError(f"destination already exists: {product}")
    files = repository_files(root)
    moved = [name for name in files if name.startswith(source + "/") and (root / name).is_file()]
    if not moved or f"{source}/BUILD.bazel" not in moved:
        raise ValueError("promotion requires a Bazel-wired Python example")
    dependency_packages = {"python/temporal_agent_harness/web"}
    for name in moved:
        if name.endswith("BUILD.bazel"):
            dependency_packages.update(re.findall(r'"//([^":]+):', (root / name).read_text()))
    executable_destinations = {destination + name[len(source):] for name in moved if os.access(root / name, os.X_OK)}
    changes: dict[str, tuple[str | None, str | None]] = {}
    for name in files:
        path = root / name
        if not path.is_file():
            continue
        if path.is_symlink():
            if name in moved:
                raise ValueError("promotion does not copy symlinks")
            continue
        try:
            before = path.read_text()
        except UnicodeDecodeError:
            if name in moved:
                raise ValueError("first milestone supports text-only example assets")
            continue
        after = before.replace(source, destination)
        if name == "python/examples/BUILD.bazel":
            after = after.replace(f'    "{candidate.example}",\n', "")
        # Generated Python visibility admits product callers as well as example/test callers.
        if (name in moved or name.removesuffix("/BUILD.bazel") in dependency_packages) and name.endswith("BUILD.bazel") and '"//python:__subpackages__"' in after:
            after = after.replace('"//python:__subpackages__"', '"//python:__subpackages__", "//products:__subpackages__"')
            # Keep future Gazelle runs from undoing the product visibility.
            if name not in moved:
                after += '\n# gazelle:python_default_visibility //python:__subpackages__,//products:__subpackages__\n'
        if name in moved:
            if name.endswith(".md"):
                # Rebase local documentation links whose targets did not move with the example.
                def rebase_link(match: re.Match[str]) -> str:
                    link = match.group(1)
                    if "://" in link or link.startswith(("#", "/")):
                        return match.group(0)
                    path_part, separator, fragment = link.partition("#")
                    old_target = (path.parent / path_part).resolve()
                    if not old_target.exists() or not old_target.is_relative_to(root.resolve()):
                        return match.group(0)
                    target_name = old_target.relative_to(root.resolve()).as_posix()
                    if target_name == source or target_name.startswith(source + "/"):
                        return match.group(0)
                    new_parent = root / (destination + name[len(source):])
                    relative = os.path.relpath(old_target, new_parent.parent)
                    return "](" + relative + (separator + fragment if separator else "") + ")"
                after = re.sub(r"\]\(([^)\s]+)\)", rebase_link, after)
            if name.endswith("/justfile"):
                after = after.replace("../../../", "../../../../")
                after = after.replace('root := justfile_directory() / ".." / ".." / ".."', 'root := justfile_directory() / ".." / ".." / ".." / ".."')
                after = after.replace(f'just --justfile "{{{{root}}}}/justfile" dev {candidate.example}', f'bazel run //{product}/examples:dev')
            changes[name] = (before, None)
            changes[destination + name[len(source):]] = (None, after)
        elif before != after:
            changes[name] = (before, after)
    # Standalone product development entry point. Dependencies remain in this monorepo.
    changes[f"{product}/examples/BUILD.bazel"] = (None, f'''load("//bazel:dev.bzl", "harness_dev")

harness_dev(
    name = "dev",
    cli = "//python/temporal_agent_harness/web:cli",
    examples = ["{candidate.example}"],
    server = True,
    session_manager = True,
    workers = True,
)
''')
    changes[f"{product}/BUILD.bazel"] = (None, '''# gazelle:python_root
# gazelle:python_manifest_file_name ../../python/gazelle_python.yaml
# gazelle:python_generation_mode file
# gazelle:python_test_file_pattern test_*.py
# gazelle:python_generate_pyi_deps false
# gazelle:map_kind py_library py_library @aspect_rules_py//py:defs.bzl
# gazelle:map_kind py_binary harness_module_binary //bazel:binary.bzl
# gazelle:map_kind py_test harness_pytest_test //bazel:pytest.bzl
# gazelle:python_default_visibility //python:__subpackages__,//products:__subpackages__
''')
    changes[f"{product}/README.md"] = (None, f'''# {candidate.name}

Owner: {candidate.owner}

{candidate.problem}

Declared ready by {declaration.declared_by} on {declaration.declared_at}.

{declaration.rationale}

Run `bazel run //{product}/examples:dev` against the configured Temporal server.
The implementation lives in `examples/{candidate.example}` within this product; its Python
import namespace is preserved for compatibility. This product uses monorepo dependencies.
See that directory's README for credentials and setup. Read `readiness.json` and `fit.json`
with `evaluation.json` for the exact source and evidence behind the declaration.
''')
    for filename, model in (("readiness.json", declaration), ("fit.json", fit), ("evaluation.json", artifact)):
        changes[f"{product}/{filename}"] = (None, model.model_dump_json(indent=2) + "\n")
    return "".join(_diff(name, before, after, executable=name in executable_destinations or ((root / name).exists() and os.access(root / name, os.X_OK)))
                   for name, (before, after) in sorted(changes.items()))
