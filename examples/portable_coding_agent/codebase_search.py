"""Semantic codebase search.

The agent queries the repository by meaning, not just by text. Files are split
into chunks, each chunk is embedded once and cached by a hash of its content, and
a query is answered by cosine-nearest chunks. Caching by content hash makes
re-indexing incremental: only chunks whose text changed are re-embedded (the
same idea as a merkle tree over the tree's contents).

The embedder is injectable so the machinery is testable offline with a
deterministic stub; production uses OpenAI embeddings.

The search reads the project on the worker's filesystem (``CODING_AGENT_WORKSPACE``),
which is the same directory mounted into the sandbox, so what the agent searches
and what it edits are the same files.

NB: no ``from __future__ import annotations``; the tool annotations build the
model-facing schema at runtime.
"""

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Callable, Sequence

from temporal_agent_harness.harness import agent

# Injectable so tests pass a stub. An embedder maps texts to vectors.
Embedder = Callable[[Sequence[str]], "list[list[float]]"]

_CHUNK_LINES = 60
_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".mypy_cache", "dist", "build"}
_TEXT_MAX_BYTES = 200_000  # skip anything larger; not source
_DEFAULT_TOP_K = 8


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


def _walk_text_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
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
        # (path, start, end, hash), rebuilt each index() from the current tree.
        self._chunks: list[tuple[str, int, int, str]] = []

    def index(self) -> int:
        """Scan the tree, embedding any chunk whose content hash is not cached.
        Returns the number of newly embedded chunks."""
        self._chunks = []
        to_embed: dict[str, str] = {}
        for path, text in _walk_text_files(self._root):
            rel = str(path.relative_to(self._root))
            for start, end, body in _chunks(text):
                h = hashlib.sha256(body.encode()).hexdigest()
                self._chunks.append((rel, start, end, h))
                if h not in self._vectors and h not in to_embed:
                    to_embed[h] = body
        if to_embed:
            hashes = list(to_embed)
            vectors = self._embedder([to_embed[h] for h in hashes])
            for h, vec in zip(hashes, vectors):
                self._vectors[h] = list(vec)
            self._persist()
        return len(to_embed)

    def _persist(self) -> None:
        if not self._cache_path:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._vectors))
        tmp.replace(self._cache_path)

    def search(self, query: str, k: int = _DEFAULT_TOP_K) -> list[tuple[str, int, int, float]]:
        """Return the top-k (path, start_line, end_line, score) for a query."""
        [qvec] = self._embedder([query])
        scored = [
            (path, start, end, _cosine(qvec, self._vectors[h]))
            for path, start, end, h in self._chunks
            if h in self._vectors
        ]
        scored.sort(key=lambda t: t[3], reverse=True)
        return scored[:k]


def format_hits(root: Path, hits: list[tuple[str, int, int, float]]) -> str:
    if not hits:
        return "no matches"
    lines = []
    for rel, start, end, score in hits:
        lines.append(f"{rel}:{start}-{end}  (score {score:.2f})")
    return "\n".join(lines)


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


@agent.activity_tool_defn(inherently_safe=True)
async def codebase_search(query: str) -> str:
    """Search the repository by MEANING for code relevant to a query, e.g. "where are retries
    configured" or "the function that validates the token". Returns the most relevant file regions
    as `path:start-end`; read those regions to see the code. Use it to find where something lives
    before reading or editing; it complements plain text search."""
    root = _workspace_root()
    index = CodebaseIndex(root, _openai_embedder(), cache_path=_cache_path(root))
    index.index()
    return format_hits(root, index.search(query))


SEARCH_TOOLS = [codebase_search]
