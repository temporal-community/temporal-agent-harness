"""Reproducible probe: GEAP RAG Engine, end to end — the replacement for `file_search`.

Run it:
    uv run --group examples python -m examples.hello_gemini_enterprise.rag_engine_probe \\
        --project <project> [--location us-west1]

GEAP refuses the Gemini Interactions API's built-in ``file_search`` (see README.md), so a doc-QA
agent moving to GEAP has to rebuild retrieval on ``Tool(retrieval=…)``. This script proves that
path works and, more usefully, pins down the constraints that are NOT obvious from the docs — each
one cost a failed attempt to discover.

It is a standalone script rather than a harness agent on purpose: everything here is *setup* that
happens outside any workflow (create corpus, ingest, verify retrieval). Once a corpus exists,
wiring it into an agent is one extra tool in ``GenerateContentConfig`` — see
``workflow_generate_content.py``, which is the agent half.

``google-genai`` can *use* a RAG corpus but cannot create one or import files into it — that lives
in ``google-cloud-aiplatform``, which this repo does not depend on. So the corpus management here
goes straight at the REST API with ``httpx`` + ADC (both already present). That is itself part of
the finding: on GEAP, ingestion is not in the SDK you use for inference.

MEASURED CONSTRAINTS (2026-08-10, google-genai 2.8.0):

1. **Serverless vs Spanner is a PROJECT-level decision**, not per-corpus — you PATCH
   ``projects/<p>/locations/<l>/ragEngineConfig``. And ``us-central1`` / ``us-east1`` /
   ``us-east4`` refuse Spanner mode for new projects on capacity grounds, telling you to switch
   the project to Serverless or pick another region. So there is an infrastructure decision to
   make before the first corpus exists.
2. **Corpora are regional; ``global`` is not a RAG location.**
3. **The inference call must be in the SAME region as the corpus — or retrieval SILENTLY
   NO-OPS.** A client at ``location="global"`` querying a ``us-west1`` corpus returned a fluent
   answer from parametric knowledge, said it had no such information, and raised NOTHING. This is
   the sharpest edge in the whole path: a misconfigured region does not fail, it just quietly
   stops retrieving. (Same silence for a nonexistent datastore.) Anything built on this needs a
   liveness assertion like the one below.
4. **Model availability is regional, and it constrains the pair.** ``gemini-3.5-flash`` is served
   at ``global`` but 404s in ``us-west1``; ``gemini-2.5-flash`` works there. Since the corpus
   forces a region, the region can force an older model.
5. Corpus creation is a long-running operation whose first ``ragCorpora.list`` can lag; the
   ``upload_rag_file_config.rag_file_chunking_config`` shape in older docs is rejected by v1
   (omit it and take the defaults).

The proof is the same trick the OpenAI example uses: a fact no model could know
(``ZARNAK_ANSWER``). If it comes back, retrieval demonstrably ran — which, given constraint 3, is
the only trustworthy way to tell.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import google.auth
import httpx
from google import genai
from google.genai import types
from google.auth.transport.requests import Request

CORPUS_DISPLAY_NAME = "harness-geap-rag-probe"
DOC_NAME = "zarnak.md"
ZARNAK_ANSWER = "47.3"
DOC_TEXT = f"""\
# Harness internal benchmark notes (fictional)

The *Zarnak coefficient* is our internal shorthand for the median per-turn scheduling overhead a
harness agent pays on top of its model call.

For the 2026-Q3 reference build the Zarnak coefficient is **{ZARNAK_ANSWER} milliseconds**.
"""
QUESTION = "What is the Zarnak coefficient for the 2026-Q3 reference build?"
# Regional model choice — see constraint 4. gemini-3.5-flash is NOT served in us-west1.
REGIONAL_MODEL = "gemini-2.5-flash"


def _auth_header() -> dict[str, str]:
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(Request())
    return {"Authorization": f"Bearer {creds.token}"}


def ensure_corpus(client: httpx.Client, headers: dict[str, str], project: str, location: str) -> str:
    """Return the probe corpus's resource name, creating it if absent (idempotent by display name)."""
    base = f"https://{location}-aiplatform.googleapis.com/v1"
    listed = client.get(
        f"{base}/projects/{project}/locations/{location}/ragCorpora", headers=headers
    )
    listed.raise_for_status()
    for corpus in listed.json().get("ragCorpora", []):
        if corpus.get("displayName") == CORPUS_DISPLAY_NAME:
            print(f"  reusing corpus {corpus['name']}")
            return corpus["name"]

    created = client.post(
        f"{base}/projects/{project}/locations/{location}/ragCorpora",
        headers=headers,
        json={"displayName": CORPUS_DISPLAY_NAME, "description": "harness GEAP RAG probe"},
    )
    if created.status_code >= 300:
        sys.exit(
            f"  corpus creation failed [{created.status_code}]: {created.text}\n"
            "  If this mentions Spanner capacity, either switch the PROJECT to Serverless mode\n"
            "  (PATCH .../ragEngineConfig with {'ragManagedDbConfig': {'serverless': {}}}) or\n"
            "  pick another region — see constraint 1 in this module's docstring."
        )
    print("  creation started (long-running); waiting for the corpus to appear…")
    deadline = time.time() + 600
    while time.time() < deadline:
        listed = client.get(
            f"{base}/projects/{project}/locations/{location}/ragCorpora", headers=headers
        )
        for corpus in listed.json().get("ragCorpora", []):
            if corpus.get("displayName") == CORPUS_DISPLAY_NAME:
                print(f"  corpus ready: {corpus['name']}")
                return corpus["name"]
        time.sleep(20)
    sys.exit("  corpus never appeared within 10 minutes")


def ensure_document(
    client: httpx.Client, headers: dict[str, str], corpus: str, location: str
) -> None:
    """Upload the probe document unless the corpus already has files."""
    base = f"https://{location}-aiplatform.googleapis.com/v1"
    listed = client.get(f"{base}/{corpus}/ragFiles", headers=headers)
    if listed.status_code == 200 and listed.json().get("ragFiles"):
        print(f"  corpus already has {len(listed.json()['ragFiles'])} file(s)")
        return

    # No chunking config: the shape older docs show is rejected by v1, and the defaults are fine.
    uploaded = client.post(
        f"https://{location}-aiplatform.googleapis.com/upload/v1/{corpus}/ragFiles:upload",
        headers=headers,
        files={
            "metadata": (None, json.dumps({"rag_file": {"display_name": DOC_NAME}}), "application/json"),
            "file": (DOC_NAME, DOC_TEXT.encode("utf-8"), "text/markdown"),
        },
    )
    if uploaded.status_code >= 300:
        sys.exit(f"  upload failed [{uploaded.status_code}]: {uploaded.text}")
    print("  uploaded; waiting for indexing…")
    for _ in range(12):
        listed = client.get(f"{base}/{corpus}/ragFiles", headers=headers)
        if listed.status_code == 200 and listed.json().get("ragFiles"):
            print("  indexed")
            return
        time.sleep(5)
    print("  WARNING: file not visible yet; the query below may miss")


def query(project: str, location: str, corpus: str, model: str) -> bool:
    """Ask the corpus-only question. Returns whether retrieval demonstrably fed the model."""
    client = genai.Client(enterprise=True, project=project, location=location)
    response = client.models.generate_content(
        model=model,
        contents=QUESTION,
        config=types.GenerateContentConfig(
            tools=[
                types.Tool(
                    retrieval=types.Retrieval(
                        vertex_rag_store=types.VertexRagStore(
                            rag_corpora=[corpus], similarity_top_k=5
                        )
                    )
                )
            ]
        ),
    )
    text = response.text or ""
    print(f"  reply: {text[:220]!r}")
    return ZARNAK_ANSWER in text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--location",
        default="us-west1",
        help="RAG corpus + inference region. NOT 'global' (constraint 2/3).",
    )
    parser.add_argument("--model", default=REGIONAL_MODEL)
    parser.add_argument(
        "--demo-silent-failure",
        action="store_true",
        help="Also query from location='global' to demonstrate that a region mismatch "
        "silently returns no retrieval rather than erroring (constraint 3).",
    )
    args = parser.parse_args()

    if args.location == "global":
        sys.exit("error: 'global' is not a RAG location — pass a region (e.g. us-west1).")

    headers = _auth_header()
    with httpx.Client(timeout=180) as client:
        print("1. corpus")
        corpus = ensure_corpus(client, headers, args.project, args.location)
        print("2. document")
        ensure_document(client, headers, corpus, args.location)

    print(f"3. query in {args.location} with {args.model}")
    ok = query(args.project, args.location, corpus, args.model)
    print("   => RETRIEVAL PROVEN" if ok else "   => NO RETRIEVAL (figure absent)")

    if args.demo_silent_failure:
        print("4. same corpus, client at location='global' (expected: silent no-op)")
        try:
            leaked = query(args.project, "global", corpus, "gemini-3.5-flash")
            print(
                "   => retrieval worked cross-region (constraint 3 no longer holds!)"
                if leaked
                else "   => SILENTLY returned no retrieval, and raised nothing — constraint 3"
            )
        except Exception as exc:  # noqa: BLE001 - the whole point is whether it raises
            print(f"   => raised (better than silence): {str(exc)[:160]}")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
