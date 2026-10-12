"""Reading and writing Open Knowledge Format (OKF) bundles.

An OKF bundle is a directory of markdown files. ``index.md`` (a directory listing) and ``log.md``
(a dated change history) are reserved at any level; every other ``.md`` file is a **concept**:
YAML frontmatter between ``---`` lines, whose only required key is ``type``, then a markdown
body. Concepts link to each other with ordinary markdown links. See
https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md (v0.2).

Everything here reads permissively, as OKF asks of consumers: a file with missing or broken
frontmatter is still a concept, of type ``Unknown``, with the problem recorded, and a link to a
concept that doesn't exist is kept and marked dangling. Nothing an agent writes is rejected.

:func:`build_okf_graph` walks a bundle through a filesystem's ``list``, ``stat`` and ``read``;
the ``vfs.<name>.okf_graph`` activity every ``ActivityFileSystem`` gets runs it.
:func:`render_concept` writes a concept's text, in the workflow.
"""

# No `from __future__ import annotations`: the models below are host-function results, and the
# Code Mode stubs need their field types as real type objects inside the workflow sandbox.
import posixpath
import re
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol
from urllib.parse import unquote

from pydantic import BaseModel, JsonValue
from temporalio import workflow

from .vfs import FileStat

with workflow.unsafe.imports_passed_through():
    import yaml

__all__ = [
    "OKFConcept",
    "OKFGraph",
    "OKFLink",
    "OKF_VERSION",
    "build_okf_graph",
    "render_concept",
]

OKF_VERSION = "0.2"

RESERVED_NAMES = frozenset({"index.md", "log.md"})

# The most a walk reads, so a huge or runaway bundle can't stall the activity or the console.
MAX_OKF_CONCEPTS = 1_000
MAX_OKF_FILE_BYTES = 256_000
MAX_OKF_TOTAL_BYTES = 8_000_000
# Without links only a concept's frontmatter is needed, so a walk reads just each file's head,
# and reads on only when the frontmatter hasn't closed within it.
OKF_HEAD_BYTES = 8_192

TrustTier = Literal["unverified", "machine-confirmed", "human-reviewed"]


class OKFConcept(BaseModel):
    """One concept, as its frontmatter describes it. ``problem`` says what is wrong with a
    concept whose frontmatter is missing or broken; such a concept is still listed, with type
    ``Unknown`` unless its frontmatter named one."""

    id: str
    path: str
    type: str
    title: str
    description: str = ""
    tags: list[str] = []
    status: str = "stable"
    trust_tier: TrustTier = "unverified"
    stale: bool = False
    size: int
    problem: str | None = None


class OKFLink(BaseModel):
    """A markdown link from one concept to another, by concept id. ``dangling`` when no
    concept in the bundle has the target id (or the walk stopped before reaching it)."""

    source: str
    target: str
    dangling: bool = False


class OKFGraph(BaseModel):
    """A bundle's concepts and, when asked for, the links between them. ``truncated`` when a
    limit stopped the walk early. ``walked_at`` is a Unix timestamp."""

    concepts: list[OKFConcept]
    links: list[OKFLink] = []
    truncated: bool = False
    walked_at: float


class _Readable(Protocol):
    async def stat(self, path: PurePosixPath) -> FileStat | None: ...
    async def read(
        self, path: PurePosixPath, offset: int = 0, length: int | None = None
    ) -> bytes: ...
    async def list(self, path: PurePosixPath) -> list[str]: ...


# ---------------------------------------------------------------- one concept

_UNCLOSED = "frontmatter is never closed with a `---` line"


def split_frontmatter(text: str) -> tuple[str | None, str, str | None]:
    """``(frontmatter, body, problem)``: the YAML between the first two ``---`` lines and the
    text after it, or ``None`` and the whole text with the reason there is no frontmatter."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, text, "no frontmatter: the file doesn't start with a `---` line"
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[1:i]), "".join(lines[i + 1 :]), None
    return None, text, _UNCLOSED


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _tags(value: object) -> list[str]:
    """The tags, each once, in the order first given."""
    if isinstance(value, list):
        tags = [_text(t) for t in value]
    elif isinstance(value, str):
        tags = [t.strip() for t in value.split(",")]
    else:
        return []
    return list(dict.fromkeys(t for t in tags if t))


def _trust_tier(verified: object) -> TrustTier:
    """OKF §5.3: a bare ``verified`` mapping counts as a one-element list."""
    events = [verified] if isinstance(verified, dict) else verified
    if not isinstance(events, list):
        return "unverified"
    actors = [_text(e.get("by")) for e in events if isinstance(e, dict) and _text(e.get("by"))]
    if any(actor.startswith("human:") for actor in actors):
        return "human-reviewed"
    return "machine-confirmed" if actors else "unverified"


def _instant(value: object) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def parse_concept(
    concept_id: str, path: str, text: str, *, size: int, now: datetime
) -> tuple[OKFConcept, str]:
    """A concept and its body, from its file's text. Never raises for bad content."""
    block, body, problem = split_frontmatter(text)
    fields: dict[str, Any] = {}
    if block is not None:
        try:
            loaded = yaml.safe_load(block)
        except yaml.YAMLError as e:
            problem = f"frontmatter is not valid YAML: {_first_line(e)}"
        else:
            if isinstance(loaded, dict):
                fields = {str(k): v for k, v in loaded.items()}
            elif loaded is None:
                problem = "frontmatter is empty"
            else:
                problem = "frontmatter is not a mapping of keys to values"
    kind = _text(fields.get("type"))
    if not kind and problem is None:
        problem = "frontmatter has no `type`"
    stale_after = _instant(fields.get("stale_after"))
    concept = OKFConcept(
        id=concept_id,
        path=path,
        type=kind or "Unknown",
        title=_text(fields.get("title")) or PurePosixPath(path).stem,
        description=_text(fields.get("description")),
        tags=_tags(fields.get("tags")),
        status=_text(fields.get("status")) or "stable",
        trust_tier=_trust_tier(fields.get("verified")),
        stale=stale_after is not None and now >= stale_after,
        size=size,
        problem=problem,
    )
    return concept, body


def _first_line(error: Exception) -> str:
    return str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__


# ---------------------------------------------------------------- links

_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def link_targets(body: str, concept_path: str) -> list[str]:
    """The concept ids ``body`` links to, in order, without repeats: links to ``.md`` files in
    the bundle, outside fenced code blocks. ``concept_path`` is the linking file's path in the
    bundle, against which relative targets resolve; a target starting with ``/`` is relative
    to the bundle root."""
    found: list[str] = []
    here = PurePosixPath(concept_path).parent
    fence: str | None = None
    for line in body.splitlines():
        opened = _FENCE.match(line)
        if opened:
            marker = opened.group(1)
            fence = None if fence == marker else (fence or marker)
            continue
        if fence:
            continue
        for match in _LINK.finditer(line):
            target = _resolve(unquote(match.group(1)), here)
            if target is not None and target not in found:
                found.append(target)
    self_id = concept_path.removesuffix(".md")
    return [t for t in found if t != self_id]


def _resolve(target: str, here: PurePosixPath) -> str | None:
    target = target.split("#", 1)[0]
    if not target or _SCHEME.match(target) or not target.endswith(".md"):
        return None
    joined = target.lstrip("/") if target.startswith("/") else str(here / target)
    normal = posixpath.normpath(joined)
    if normal in (".", "") or normal.startswith("../") or normal == "..":
        return None
    if PurePosixPath(normal).name in RESERVED_NAMES:
        return None
    return normal.removesuffix(".md")


# ---------------------------------------------------------------- the walk


async def build_okf_graph(
    fs: _Readable, *, prefix: PurePosixPath, links: bool, now: datetime
) -> OKFGraph:
    """Every concept under ``prefix`` (mount-relative; ``.`` for the whole bundle), read
    through ``fs``, and the links between them when ``links`` is set. Directories and files are
    visited in sorted order, so the same bundle gives the same graph.

    A directory that can't be listed, or an entry that can't be stat'ed, is skipped rather than
    failing the walk; a concept that can't be read is listed with the problem."""
    concepts: list[OKFConcept] = []
    bodies: dict[str, str] = {}
    total = 0
    truncated = False

    async def read_concept(path: PurePosixPath, size: int) -> None:
        nonlocal total, truncated
        budget = min(MAX_OKF_FILE_BYTES, MAX_OKF_TOTAL_BYTES - total)
        if budget <= 0:
            truncated = True
            return
        key = str(path)
        concept_id = key.removesuffix(".md")
        try:
            if links:
                data = await fs.read(path, 0, budget)
            else:
                data = await fs.read(path, 0, min(OKF_HEAD_BYTES, budget))
                head = data.decode("utf-8", errors="replace")
                if len(data) < size and split_frontmatter(head)[2] == _UNCLOSED:
                    data = await fs.read(path, 0, budget)
        except OSError as e:
            concepts.append(
                OKFConcept(
                    id=concept_id,
                    path=key,
                    type="Unknown",
                    title=path.stem,
                    size=size,
                    problem=f"unreadable: {e.strerror or e}",
                )
            )
            return
        total += len(data)
        concept, body = parse_concept(
            concept_id, key, data.decode("utf-8", errors="replace"), size=size, now=now
        )
        concepts.append(concept)
        if links:
            bodies[concept_id] = body

    async def walk(directory: PurePosixPath) -> None:
        nonlocal truncated
        try:
            names = sorted(await fs.list(directory))
        except OSError:
            return
        for name in names:
            if truncated:
                return
            path = PurePosixPath(name) if str(directory) == "." else directory / name
            try:
                found = await fs.stat(path)
            except OSError:
                continue
            if found is None:
                continue
            if found.is_dir:
                await walk(path)
            elif name.endswith(".md") and name not in RESERVED_NAMES:
                if len(concepts) >= MAX_OKF_CONCEPTS:
                    truncated = True
                    return
                await read_concept(path, found.size)

    await walk(prefix)

    edges: list[OKFLink] = []
    if links:
        known = {c.id for c in concepts}
        for concept in concepts:
            for target in link_targets(bodies.get(concept.id, ""), concept.path):
                edges.append(
                    OKFLink(source=concept.id, target=target, dangling=target not in known)
                )
    return OKFGraph(
        concepts=concepts, links=edges, truncated=truncated, walked_at=now.timestamp()
    )


# ---------------------------------------------------------------- writing


def render_concept(
    frontmatter: dict[str, JsonValue], body: str, *, generated_by: str, at: datetime
) -> str:
    """A concept's full text: ``frontmatter`` as YAML between ``---`` lines, then ``body``.

    Records ``generated: {by: <generated_by>, at: <at>}`` unless the frontmatter already has
    ``generated``, and gives a ``verified`` entry without ``at`` the time ``at`` (a script can't
    read the clock itself). Renders whatever it is given; a frontmatter without ``type`` is not
    refused."""
    stamp = at.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    fields: dict[str, Any] = dict(frontmatter)
    if "generated" not in fields:
        fields["generated"] = {"by": generated_by, "at": stamp}
    verified = fields.get("verified")
    if isinstance(verified, dict):
        fields["verified"] = {**verified, "at": verified.get("at", stamp)}
    elif isinstance(verified, list):
        fields["verified"] = [
            {**v, "at": v.get("at", stamp)} if isinstance(v, dict) else v for v in verified
        ]
    block = yaml.safe_dump(fields, sort_keys=False, allow_unicode=True, default_flow_style=False)
    text = body.lstrip("\n")
    return f"---\n{block}---\n\n{text}" + ("" if text.endswith("\n") else "\n")
