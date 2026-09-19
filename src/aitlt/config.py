"""Config loading.

Kept deliberately dumb: a YAML file describing how to talk to one chat endpoint,
plus per-tenant auth pulled from the environment. Tokens never live in the file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    pass


def _expand(value: Any) -> Any:
    """Replace ${VAR} with the environment value, recursively."""
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            got = os.environ.get(name)
            if got is None:
                raise ConfigError(f"config references ${{{name}}} but that environment variable is not set")
            return got

        return _ENV_REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


class TenantAuth(BaseModel):
    """How to authenticate as one tenant's low-privilege test user."""

    #: Header name/value pairs merged into the request for this tenant.
    headers: dict[str, str] = Field(default_factory=dict)
    #: Extra body fields merged into the request for this tenant (for APIs that
    #: take the tenant in the payload rather than a header).
    body: dict[str, Any] = Field(default_factory=dict)


class StreamingConfig(BaseModel):
    """Opt-in SSE support. Off unless the target actually streams.

    Deliberately minimal: the tool buffers the whole stream and folds it back
    into the ordinary answer/citations/metadata contract. Everything downstream
    — detection, severity, reporting — is unchanged, because a leak in a
    streamed answer is the same leak.
    """

    mode: Literal["none", "sse"] = "none"
    #: Dotted path *within each frame* to the incremental text chunk.
    #: OpenAI-style: "choices.0.delta.content". Simple APIs: "delta" or "token".
    delta_field: str = "delta"
    #: Frame payload that ends the stream. Set to null if the API sends none.
    done_sentinel: str | None = "[DONE]"


class EndpointConfig(BaseModel):
    """The one supported target shape for V1.

    A single JSON-over-HTTP chat endpoint that takes a prompt in the request
    body, identifies the caller's tenant from a header or body field, and
    returns an answer plus optional citations and metadata.

    Deliberately not generalised. Every additional shape is a connector that
    has to be maintained, and a pilot that turns into custom consulting.
    """

    url: str
    method: str = "POST"
    #: Sent on every request, for every tenant. Per-tenant auth goes in `tenants`.
    headers: dict[str, str] = Field(default_factory=dict)
    #: Where the prompt goes in the request body. Dotted: "input.message".
    prompt_field: str = "message"
    #: Static body fields merged into every request.
    body: dict[str, Any] = Field(default_factory=dict)
    #: Dotted read path to the assistant's answer text. Required.
    response_text_field: str = "answer"
    #: Dotted read path to citations. Set to null if the API returns none —
    #: leaving it pointing at a field that does not exist means citation tests
    #: scan nothing and pass vacuously. `verify-ingest` checks for this.
    citations_field: str | None = "citations"
    #: Dotted read path to metadata returned to the client. Same warning.
    metadata_field: str | None = "metadata"
    timeout_seconds: float = 60.0
    #: Retries on transport errors and 5xx only. A successful response is never
    #: re-sent, so cache tests stay honest.
    max_retries: int = 2
    retry_backoff_seconds: float = 0.5
    #: SSE support, off by default. When off, a streamed response is refused
    #: rather than scanned as one undifferentiated blob.
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)


class ReportConfig(BaseModel):
    formats: list[str] = Field(default_factory=lambda: ["markdown", "json"])
    directory: str = "output"


class Config(BaseModel):
    endpoint: EndpointConfig
    tenants: dict[str, TenantAuth]
    report: ReportConfig = Field(default_factory=ReportConfig)
    environment: str = "staging"
    build_id: str | None = None
    #: Where fixture JSON lives.
    fixtures_path: str = "fixtures/fixtures.json"

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"no config at {p}. Run `aitenant init` first.")
        raw = yaml.safe_load(p.read_text()) or {}
        return cls.model_validate(_expand(raw))


def dig(data: Any, dotted: str | None) -> Any:
    """Read a dotted path out of nested dicts/lists. Returns None if absent."""
    if not dotted:
        return None
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def bury(data: dict[str, Any], dotted: str, value: Any) -> None:
    """Write a value into a nested dict, creating intermediate dicts."""
    parts = dotted.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


DEFAULT_CONFIG = """\
# AI Tenant Leak Test configuration.
# Tokens are read from the environment — never commit them here.

environment: staging
build_id: null

endpoint:
  url: http://127.0.0.1:8000/ai/chat
  method: POST
  prompt_field: message
  response_text_field: answer
  citations_field: citations
  metadata_field: metadata
  headers:
    Content-Type: application/json

# One low-privilege test user per tenant. Whatever identifies the caller's
# tenant to your API goes here.
tenants:
  tenant_a:
    headers:
      Authorization: Bearer ${TENANT_A_TOKEN}
  tenant_b:
    headers:
      Authorization: Bearer ${TENANT_B_TOKEN}

report:
  formats: [markdown, json]
  directory: output

fixtures_path: fixtures/fixtures.json
"""
