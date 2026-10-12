"""``okf_code_mode_tool``: a Code Mode tool preconfigured for one OKF bundle.

It is :func:`~.tool.code_mode_tool` with the bundle's mount added, three host functions for
working with the bundle, and a section of the tool's contract that explains OKF to the model:

- ``okf_concepts(prefix)``: every concept's frontmatter, from one ``vfs.<name>.okf_graph``
  activity instead of a file read per concept;
- ``okf_links(id)``: a concept's links and backlinks, from the same activity;
- ``okf_render(frontmatter, body)``: a concept's text with well-formed YAML frontmatter and
  ``generated`` filled in, for the script to write with ``pathlib`` like any file.

Reading and writing concepts stay ordinary file operations on the mount, so they are indexed,
shown in the console and subject to the approval policy. Nothing a script writes is rejected.
"""

# No `from __future__ import annotations`: the host functions' annotations must be real type
# objects for the Code Mode stubs, which can't resolve strings inside the workflow sandbox.
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, JsonValue
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.agent_workflow import Injected, tool_defn

    from .activity_fs import ActivityBackend
    from .okf import OKFConcept, OKFLink, render_concept
    from .tool import _code_mode_tool
    from .vfs import IndexedVFSMount, VFSMount

__all__ = ["OKFLinks", "okf_code_mode_tool"]

# The injection the OKF host functions receive the bundle's backend under. Reserved: a
# developer's own injection of this name would be ambiguous.
BUNDLE_INJECTION = "okf_bundle"


@dataclass(frozen=True)
class _Bundle:
    """What the OKF host functions are injected with: the bundle's backend and mount path."""

    backend: ActivityBackend
    mount: str


class OKFLinks(BaseModel):
    """A concept's links to other concepts, and the concepts that link to it."""

    id: str
    links_to: list[OKFLink]
    cited_by: list[OKFLink]


def _mounted(concept: OKFConcept, mount: str) -> OKFConcept:
    return concept.model_copy(update={"path": f"{mount}/{concept.path}"})


@tool_defn(inherently_safe=True)
async def okf_concepts(okf_bundle: Injected[_Bundle], prefix: str = "") -> list[OKFConcept]:
    """Every concept in the OKF bundle, from its frontmatter, without reading bodies. `prefix`
    limits it to the concepts under a folder, by concept id (e.g. "trips"). Each concept's
    `path` is the file to read; `problem` says what is wrong with a concept whose frontmatter
    is missing or broken."""
    start = PurePosixPath(prefix.strip("/") or ".")
    graph = await okf_bundle.backend.okf_graph(start, links=False)
    return [_mounted(c, okf_bundle.mount) for c in graph.concepts]


@tool_defn(inherently_safe=True)
async def okf_links(okf_bundle: Injected[_Bundle], id: str) -> OKFLinks:
    """The concepts the concept `id` links to, and the concepts that link to it (by concept
    id). `dangling` marks a link to a concept that doesn't exist yet."""
    graph = await okf_bundle.backend.okf_graph(PurePosixPath("."), links=True)
    concept_id = id.strip("/").removesuffix(".md")
    return OKFLinks(
        id=concept_id,
        links_to=[link for link in graph.links if link.source == concept_id],
        cited_by=[link for link in graph.links if link.target == concept_id],
    )


@tool_defn(inherently_safe=True)
async def okf_render(frontmatter: dict[str, JsonValue], body: str) -> str:
    """A concept's full text: `frontmatter` as YAML between `---` lines, then the markdown
    `body`. Fills in `generated` (who wrote it, and when) unless you give one, and the time of a
    `verified` entry given without `at`. Write the result to the concept's file with
    `Path(...).write_text(...)`."""
    return render_concept(
        frontmatter,
        body,
        generated_by=workflow.info().workflow_type,
        at=workflow.now(),
    )


_HOST_FUNCTIONS = [okf_concepts, okf_links, okf_render]

_GUIDE = """
Knowledge bundle: `{mount}` is an Open Knowledge Format (OKF v{version}) bundle, a folder of \
markdown files you read and write with `open()` and `pathlib` like any other.

- Every `.md` file is a concept, except `index.md` and `log.md`. A concept's id is its path in \
the bundle without `.md` (`{mount}/trips/denver.md` is `trips/denver`).
- A concept starts with YAML frontmatter between `---` lines, then a markdown body. `type` is \
required: a short descriptive kind, such as `Person` or `Plan`. Also give `title` and \
`description` (ONE sentence). Optional: `tags` (a list), `status` (`draft`, `stable` or \
`deprecated`), `stale_after` (a UTC timestamp after which the concept is out of date), \
`verified` (a list of `{{by}}`, with `by` as `human:<id>` when a person confirmed it; \
`okf_render` adds the time), and \
`sources` (a list of `{{id, resource, title}}`).
- Write concepts with `okf_render(frontmatter, body)`, which formats the frontmatter and \
records who wrote the concept and when: `Path(...).write_text(await okf_render({{...}}, body))`.
- Link related concepts with ordinary markdown links relative to the linking file, e.g. \
`[Ana](../people/ana.md)`. Link only to concepts that exist, never to the concept itself, and \
once per mention.
- Each folder has an `index.md` listing its concepts, one line each: \
`* [Title](slug.md) - description`, and subfolders as `* [folder](folder/index.md) - summary`. \
Keep it up to date whenever you add, retitle or remove a concept.
- `{mount}/log.md` records changes, newest first, under `## YYYY-MM-DD` headings: \
`* **Creation**: [Title](path.md) ...`. Add an entry for every change.
- To find concepts, call `okf_concepts()` rather than reading files one by one; then read the \
files you need. `okf_links(id)` gives a concept's links and backlinks.
- Treat concepts marked `stale` or `deprecated` as out of date, and say so when you use them.
"""


def okf_code_mode_tool(
    bundle: VFSMount,
    *,
    name: str,
    tools: Sequence[Callable[..., Awaitable[Any]]] = (),
    mounts: Sequence[VFSMount] = (),
    injections: Mapping[str, Any] | None = None,
    inherently_safe: bool = True,
    auto_approval_criteria: str | None = None,
) -> Callable[..., Awaitable[str]]:
    """A Code Mode tool for working with the OKF bundle ``bundle``.

    ``bundle`` is a bound ``agent.okf_bundle_vfs_mount(...)``. The tool is
    :func:`~.tool.code_mode_tool` over ``tools`` plus the OKF host functions, and ``mounts``
    plus the bundle, with OKF explained in its contract; every other argument means what it
    does there. ``injections`` may not use the name ``okf_bundle``, which the OKF host functions
    receive the bundle under.
    """
    if not (
        isinstance(bundle, IndexedVFSMount)
        and isinstance(bundle.backend, ActivityBackend)
        and bundle.index.current.okf_version
    ):
        raise TypeError(
            f"okf_code_mode_tool takes a bound agent.okf_bundle_vfs_mount(...), got {bundle!r}"
        )
    if injections and BUNDLE_INJECTION in injections:
        raise ValueError(
            f"injection name {BUNDLE_INJECTION!r} is reserved by okf_code_mode_tool"
        )
    guide = _GUIDE.format(mount=bundle.path, version=bundle.index.current.okf_version)
    return _code_mode_tool(
        [*tools, *_HOST_FUNCTIONS],
        name=name,
        inherently_safe=inherently_safe,
        auto_approval_criteria=auto_approval_criteria,
        injections={
            **(injections or {}),
            BUNDLE_INJECTION: _Bundle(backend=bundle.backend, mount=bundle.path),
        },
        mounts=[*mounts, bundle],
        guide=guide,
    )

