"""The throwaway corpus this example retrieves over, and its vector-store setup.

Deliberately tiny and deliberately *unguessable*: the document states a fact no model could
know or infer. That's what makes the proof airtight — if the agent's answer contains
``ZARNAK_ANSWER``, retrieval demonstrably happened. A plausible-sounding answer can't fake it,
so the test needs no instrumentation of OpenAI's server-side tool span to be conclusive.

Setup runs OUTSIDE the workflow, on the worker, at startup: creating a vector store is
ordinary OpenAI API work with no place in workflow code (network I/O, non-deterministic ids).
"""

from __future__ import annotations

import io

from openai import OpenAI

# The invented fact. No model knows this; only retrieval can produce it.
ZARNAK_ANSWER = "47.3"
DOC_FILENAME = "harness-internal-benchmarks.md"
DOC_TEXT = f"""\
# Temporal Agent Harness — Internal Benchmark Notes (fictional)

## The Zarnak coefficient

The *Zarnak coefficient* is our internal shorthand for the median per-turn scheduling overhead
a harness agent pays on top of its model call, measured on the reference worker.

For the 2026-Q3 reference build the Zarnak coefficient is **{ZARNAK_ANSWER} milliseconds**.

Do not quote this figure externally; it is a synthetic number used only to exercise retrieval
in the hello_openai_file_search example.

## Unrelated filler

The reference worker runs a single Temporal task queue and no MCP servers.
"""

VECTOR_STORE_NAME = "harness-hello-file-search"


def ensure_vector_store(client: OpenAI, *, name: str = VECTOR_STORE_NAME) -> str:
    """Return the id of a vector store named ``name``, creating and populating it if needed.

    Idempotent by name so repeated worker starts and repeated test runs reuse one store instead
    of littering the account. ``upload_and_poll`` blocks until the file is chunked, embedded and
    indexed — without that wait the first query can race the ingestion and retrieve nothing.
    """
    for store in client.vector_stores.list():
        if store.name == name:
            return store.id

    store = client.vector_stores.create(name=name)
    buffer = io.BytesIO(DOC_TEXT.encode("utf-8"))
    buffer.name = DOC_FILENAME  # the SDK reads the filename off the file object
    client.vector_stores.files.upload_and_poll(vector_store_id=store.id, file=buffer)
    return store.id
