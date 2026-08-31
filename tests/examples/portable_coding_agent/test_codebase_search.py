"""Tests for the semantic-search machinery (examples.portable_coding_agent.codebase_search).

A deterministic keyword-count embedder stands in for the real embedding model, so
ranking, incremental content-hash caching, and chunking are all exercised offline.
"""

from pathlib import Path

from examples.portable_coding_agent.codebase_search import (
    CodebaseIndex,
    _chunks,
    format_hits,
)

VOCAB = ["retry", "token", "parse", "fibonacci"]


def _stub_embed(texts):
    # Similar text -> similar vector, so cosine ranking is meaningful without a real model.
    return [[float(t.lower().count(w)) for w in VOCAB] for t in texts]


def _repo(base: Path) -> Path:
    # A subdirectory, so a cache file kept beside it is not itself indexed.
    repo = base / "repo"
    repo.mkdir(parents=True)
    (repo / "retry.py").write_text("def retry():\n    # retry with backoff, retry again\n    ...\n")
    (repo / "auth.py").write_text("def make_token():\n    return build_token()\n")
    (repo / "math_utils.py").write_text("def fibonacci(n):\n    ...\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.py").write_text("retry retry retry\n")  # must be skipped
    return repo


def test_search_ranks_the_relevant_file_first(tmp_path: Path):
    index = CodebaseIndex(_repo(tmp_path), _stub_embed)
    index.index()
    hits = index.search("how does retry backoff work", k=3)
    assert hits, "expected matches"
    assert hits[0][0] == "retry.py"


def test_search_results_carry_the_code(tmp_path: Path):
    # Results include the matched chunk body, not just line ranges, so they are usable even
    # when the tool cannot read the file back (e.g. an isolated sandbox).
    index = CodebaseIndex(_repo(tmp_path), _stub_embed)
    index.index()
    top = index.search("retry backoff", k=1)[0]
    body = top[4]
    assert "retry" in body
    assert "retry.py" in format_hits(index._root, [top])
    assert body in format_hits(index._root, [top])


def test_secret_and_lockfiles_are_skipped(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / ".env").write_text("OPENAI_API_KEY=retry-secret\n")
    (repo / "uv.lock").write_text("retry\n")
    index = CodebaseIndex(repo, _stub_embed)
    index.index()
    paths = {path for path, *_ in index.search("retry", k=10)}
    assert ".env" not in paths
    assert "uv.lock" not in paths


def test_vendored_dirs_are_skipped(tmp_path: Path):
    index = CodebaseIndex(_repo(tmp_path), _stub_embed)
    index.index()
    assert all("node_modules" not in path for path, *_ in index.search("retry", k=10))


def test_content_hash_cache_makes_reindex_incremental(tmp_path: Path):
    repo = _repo(tmp_path)
    cache = tmp_path / "cache.json"  # sibling of repo/, not indexed
    first = CodebaseIndex(repo, _stub_embed, cache_path=cache)
    embedded_first = first.index()
    assert embedded_first > 0

    # A fresh index over the same unchanged tree re-embeds nothing (all hashes cached).
    second = CodebaseIndex(repo, _stub_embed, cache_path=cache)
    assert second.index() == 0

    # Changing one file re-embeds only its chunk.
    (repo / "auth.py").write_text("def make_token():\n    return refreshed_token()\n")
    third = CodebaseIndex(repo, _stub_embed, cache_path=cache)
    assert third.index() == 1


def test_chunks_split_and_keep_line_ranges():
    text = "\n".join(f"line {i}" for i in range(1, 131))
    chunks = _chunks(text)
    assert len(chunks) >= 2
    assert chunks[0][0] == 1
    assert chunks[1][0] == chunks[0][1] + 1


def test_format_hits_empty():
    assert format_hits(Path("/x"), []) == "no matches"
