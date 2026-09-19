"""A deliberately vulnerable multi-tenant RAG app, for calibrating the tester.

This exists so the harness can be proven to work. A negative test suite that has
never produced a positive is worthless — you cannot tell "their system is safe"
from "my tests are weak". Flip a leak flag on, confirm the suite catches it; flip
them all off, confirm the suite goes green.

It doubles as sales collateral: a real failing report, from a real HTTP service,
with zero customer NDA exposure.

The "LLM" is a deterministic stub that concatenates retrieved chunks. That is
intentional — this app is a test bed for the boundary, not for generation quality.

    LEAK_POST_FILTER          retrieve across all tenants, scope only afterwards
    LEAK_CITATION_RESOLVER    scope the content but not the citation list
    LEAK_METADATA             scope the content but not owners/projects/filenames
    LEAK_SEMANTIC_CACHE       key the response cache on question text alone
    LEAK_INDIRECT_INJECTION   obey instructions found inside retrieved documents

Run:
    LEAK_POST_FILTER=1 python -m uvicorn devtarget.app:app --port 8000

NEVER deploy this anywhere reachable. It is built to leak.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

FIXTURES = Path(os.environ.get("AITLT_FIXTURES", "fixtures/fixtures.json"))

TOKEN_TO_TENANT = {
    "tenant-a-token": "tenant_a",
    "tenant-b-token": "tenant_b",
}

TOP_K = 3


def flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


app = FastAPI(title="Vulnerable RAG (test target)")

_cache: dict[str, dict[str, Any]] = {}


def load_documents() -> list[dict[str, Any]]:
    if not FIXTURES.exists():
        raise RuntimeError(f"fixtures not found at {FIXTURES}; run `aitenant init` first")
    data = json.loads(FIXTURES.read_text())
    docs: list[dict[str, Any]] = []
    for tenant in data["tenants"]:
        for doc in tenant["documents"]:
            docs.append(doc)
    return docs


DOCUMENTS = load_documents()


def tokenize(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def score(query: str, doc: dict[str, Any]) -> float:
    q = tokenize(query)
    d = tokenize(f"{doc['title']} {doc['body']}")
    if not q or not d:
        return 0.0
    return len(q & d) / len(q)


def retrieve(query: str, tenant_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (documents used for the answer, documents used for citations).

    In the secure path both are the same scoped list. The leak flags pull them
    apart, which is exactly how this fails in production: someone scopes the
    content and forgets the citation resolver.
    """
    if flag("LEAK_POST_FILTER"):
        # The bug: similarity runs over every tenant's chunks, and scoping is
        # applied to the result list after the context has already been built.
        candidates = sorted(DOCUMENTS, key=lambda d: score(query, d), reverse=True)[:TOP_K]
        answer_docs = [d for d in candidates if score(query, d) > 0]
    else:
        scoped = [d for d in DOCUMENTS if d["tenant_id"] == tenant_id]
        candidates = sorted(scoped, key=lambda d: score(query, d), reverse=True)[:TOP_K]
        answer_docs = [d for d in candidates if score(query, d) > 0]

    if flag("LEAK_CITATION_RESOLVER"):
        # Citations resolved from the raw index, ignoring the caller's scope.
        unscoped = sorted(DOCUMENTS, key=lambda d: score(query, d), reverse=True)[:TOP_K]
        citation_docs = [d for d in unscoped if score(query, d) > 0]
    else:
        citation_docs = [d for d in answer_docs if d["tenant_id"] == tenant_id]

    return answer_docs, citation_docs


def synthesize(query: str, docs: list[dict[str, Any]]) -> str:
    if not docs:
        return "I could not find anything relevant in the knowledge base."

    injected = [d for d in docs if d.get("is_malicious_injection_doc")]
    if injected and flag("LEAK_INDIRECT_INJECTION"):
        # The model treats retrieved text as instructions rather than data.
        doc = injected[0]
        titles = "\n".join(f"- {d['title']}" for d in DOCUMENTS)
        return (
            "Maintenance mode acknowledged. Full document text follows:\n\n"
            f"{doc['body']}\n\nAll documents in the index:\n{titles}"
        )

    parts = [f"From '{d['title']}':\n{d['body']}" for d in docs]
    return f"Here is what I found regarding your question.\n\n" + "\n\n".join(parts)


class ChatRequest(BaseModel):
    message: str


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "documents": len(DOCUMENTS),
        "leaks_enabled": [
            name
            for name in (
                "LEAK_POST_FILTER",
                "LEAK_CITATION_RESOLVER",
                "LEAK_METADATA",
                "LEAK_SEMANTIC_CACHE",
                "LEAK_INDIRECT_INJECTION",
            )
            if flag(name)
        ],
    }


@app.post("/ai/chat/stream")
def chat_stream(req: ChatRequest, authorization: str = Header(default="")):
    """The same answer, streamed. Same leak flags, same bugs.

    Exists so the SSE path is proven against a real socket rather than only
    against a mock: a leak must be just as detectable when the answer arrives
    as tokens, and tokenisation must not be able to split a canary so that the
    detector misses it.
    """
    payload = chat(req, authorization)

    def frames():
        # Chop the answer mid-token on purpose — a canary split across frames
        # must still be caught once the stream is reassembled.
        text = payload["answer"]
        for i in range(0, len(text), 7):
            yield f"data: {json.dumps({'delta': text[i : i + 7]})}\n\n"
        yield f"data: {json.dumps({'citations': payload['citations'], 'metadata': payload['metadata']})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(frames(), media_type="text/event-stream")


@app.post("/ai/chat")
def chat(req: ChatRequest, authorization: str = Header(default="")) -> dict[str, Any]:
    token = authorization.removeprefix("Bearer ").strip()
    tenant_id = TOKEN_TO_TENANT.get(token)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="unknown token")

    # The cache bug: the key omits the tenant, so the optimisation and the
    # isolation failure are the same mechanism.
    cache_key = req.message.strip().lower() if flag("LEAK_SEMANTIC_CACHE") else f"{tenant_id}::{req.message.strip().lower()}"
    if cache_key in _cache:
        return _cache[cache_key]

    answer_docs, citation_docs = retrieve(req.message, tenant_id)
    answer = synthesize(req.message, answer_docs)

    if flag("LEAK_METADATA"):
        metadata_docs = sorted(DOCUMENTS, key=lambda d: score(req.message, d), reverse=True)[:TOP_K]
        metadata_docs = [d for d in metadata_docs if score(req.message, d) > 0]
    else:
        metadata_docs = [d for d in answer_docs if d["tenant_id"] == tenant_id]

    response = {
        "answer": answer,
        "citations": [
            {"source_id": d["document_id"], "title": d["title"], "filename": d["filename"]} for d in citation_docs
        ],
        "metadata": {
            "sources": [
                {
                    "owner": d["metadata"].get("owner", ""),
                    "project": d["metadata"].get("project", ""),
                    "filename": d["filename"],
                }
                for d in metadata_docs
            ]
        },
    }
    _cache[cache_key] = response
    return response
