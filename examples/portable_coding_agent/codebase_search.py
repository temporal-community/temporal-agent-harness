"""Semantic codebase search.

The agent queries the repository by meaning, not just by text. Files are split
into chunks, each chunk is embedded once and cached by a hash of its content, and
a query is answered by cosine-nearest chunks. Caching by content hash makes
re-indexing incremental: only chunks whose text changed are re-embedded (the
same idea as a merkle tree over the tree's contents).

The embedder is injectable so the machinery is testable offline with a
deterministic stub; production uses OpenAI embeddings.

The search reads the project on the worker's filesystem
(``CODING_AGENT_WORKSPACE``, default the worker's cwd). That is not the same
place the ``docker`` sandbox edits files, so out of the box search can look at a
different tree than the sandbox tools touch. Two things keep it useful anyway:
the results carry the matched code (not just line ranges), and with the
``local`` backend the tools run on the host over this same directory. See the
README's coherence note.

NB: no ``from __future__ import annotations``; the tool annotations build the
model-facing schema at runtime.
"""

import asyncio
import hashlib
import json
import math
import os
from datetime import timedelta
from pathlib import Path
from typing import Callable, Sequence

from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent

# Injectable so tests pass a stub. An embedder maps texts to vectors.
Embedder = Callable[[Sequence[str]], "list[list[float]]"]

_CHUNK_LINES = 60
_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".mypy_cache", "dist", "build"}
_SKIP_FILES = {"package-lock.json", "poetry.lock", "uv.lock", "yarn.lock", "Cargo.lock"}
_TEXT_MAX_BYTES = 200_000  # skip anything larger; not source
_DEFAULT_TOP_K = 8
# OpenAI caps inputs and tokens per embeddings request, so a first index of a real
# repo cannot go in one call. Embed in batches and persist after each.
_EMBED_BATCH = 128


def _chunks(text: str) -> list[tuple[int, int, str]]:
    """Split into (start_line, end_line, text) windows of ~_CHUNK_LINES lines."""
    lines = text.split("\n")
    out: list[tuple[int, int, str]] = []
    for start in range(0, len(lines), _CHUNK_LINES):
        window = lines[start : start + _CHUNK_LINES]
        body = "\n".join(window).strip()
        if body:
            out.append((start + 1, start + len(window), "\n".join(window)))
    return out


def _skip_file(name: str) -> bool:
    # Do not embed secrets or machine-generated lockfiles into a third-party API.
    return name == ".env" or name.startswith(".env.") or name in _SKIP_FILES


def _walk_text_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if _skip_file(name):
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_size > _TEXT_MAX_BYTES:
                    continue
                yield path, path.read_text()
            except (OSError, UnicodeDecodeError):
                continue  # binary or unreadable


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class CodebaseIndex:
    """A content-hash-cached embedding index over a directory."""

    def __init__(self, root: Path, embedder: Embedder, cache_path: Path | None = None) -> None:
        self._root = root.resolve()
        self._embedder = embedder
        self._cache_path = cache_path
        # hash -> vector, persisted so re-indexing only embeds changed chunks.
        self._vectors: dict[str, list[float]] = {}
        if cache_path and cache_path.exists():
            try:
                self._vectors = json.loads(cache_path.read_text())
            except (OSError, ValueError):
                self._vectors = {}
        # (path, start, end, hash, body), rebuilt each index() from the current tree.
        self._chunks: list[tuple[str, int, int, str, str]] = []

    def index(self) -> int:
        """Scan the tree, embedding any chunk whose content hash is not cached.
        Returns the number of newly embedded chunks."""
        self._chunks = []
        to_embed: dict[str, str] = {}
        live: set[str] = set()
        for path, text in _walk_text_files(self._root):
            rel = str(path.relative_to(self._root))
            for start, end, body in _chunks(text):
                h = hashlib.sha256(body.encode()).hexdigest()
                self._chunks.append((rel, start, end, h, body))
                live.add(h)
                if h not in self._vectors and h not in to_embed:
                    to_embed[h] = body
        # Evict vectors for chunks that no longer exist (edited or deleted files), so the cache
        # tracks the tree instead of growing forever.
        stale = [h for h in self._vectors if h not in live]
        for h in stale:
            del self._vectors[h]
        if not to_embed:
            if stale:
                self._persist()
            return 0
        hashes = list(to_embed)
        # Batch so a large first index stays under the API's per-request limit, and persist
        # after each batch so a crash mid-index resumes from what was already embedded.
        for i in range(0, len(hashes), _EMBED_BATCH):
            batch = hashes[i : i + _EMBED_BATCH]
            vectors = self._embedder([to_embed[h] for h in batch])
            for h, vec in zip(batch, vectors):
                self._vectors[h] = list(vec)
            self._persist()
        return len(hashes)

    def _persist(self) -> None:
        if not self._cache_path:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._vectors))
        tmp.replace(self._cache_path)

    def search(self, query: str, k: int = _DEFAULT_TOP_K) -> list[tuple[str, int, int, float, str]]:
        """Return the top-k (path, start_line, end_line, score, body) for a query."""
        [qvec] = self._embedder([query])
        scored = [
            (path, start, end, _cosine(qvec, self._vectors[h]), body)
            for path, start, end, h, body in self._chunks
            if h in self._vectors
        ]
        scored.sort(key=lambda t: t[3], reverse=True)
        return scored[:k]


def format_hits(root: Path, hits: list[tuple[str, int, int, float, str]]) -> str:
    if not hits:
        return "no matches"
    blocks = []
    for rel, start, end, score, body in hits:
        blocks.append(f"{rel}:{start}-{end}  (score {score:.2f})\n{body}")
    return "\n\n".join(blocks)


def _workspace_root() -> Path:
    return Path(os.environ.get("CODING_AGENT_WORKSPACE", ".")).resolve()


def _cache_path(root: Path) -> Path:
    home = Path(os.environ.get("AGENT_HOME", Path.home() / ".portable-coding-agent"))
    key = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    return home / "index" / f"{key}.json"


def _openai_embedder() -> Embedder:
    from openai import OpenAI

    client = OpenAI()
    model = os.environ.get("CODING_AGENT_EMBED_MODEL", "text-embedding-3-small")

    def embed(texts: Sequence[str]) -> list[list[float]]:
        resp = client.embeddings.create(model=model, input=list(texts))
        return [d.embedding for d in resp.data]

    return embed


@agent.activity_tool_defn(
    inherently_safe=True,
    # First-time indexing walks and embeds the whole tree; the 30s default would time out on a
    # real repo. (A production index would heartbeat and run in a background service.)
    activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=10)),
)
async def codebase_search(query: str) -> str:
    """Search the repository by MEANING for code relevant to a query, e.g. "where are retries
    configured" or "the function that validates the token". Returns the most relevant file
    regions as `path:start-end` followed by the code itself, best match first. Use it to find
    where something lives before reading or editing; it complements plain text search."""
    root = _workspace_root()

    def _run() -> str:
        index = CodebaseIndex(root, _openai_embedder(), cache_path=_cache_path(root))
        index.index()
        return format_hits(root, index.search(query))

    # Indexing and embedding are blocking (file walk + the sync OpenAI client); keep them off the
    # worker's event loop so co-located sandbox activities and heartbeats are not stalled.
    return await asyncio.to_thread(_run)


SEARCH_TOOLS = [codebase_search]
