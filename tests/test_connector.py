"""Connector error-mode tests.

The connector is where this product either stays a tool or turns into
consulting. The governing requirement: a misfit endpoint must produce a fast,
named failure — never a confusing half-success that reads as a clean report.

Every test here drives a mock transport, so the error modes are exercised
without a live server.
"""

from __future__ import annotations

import httpx
import pytest

from aitlt.config import Config, EndpointConfig, TenantAuth
from aitlt.connector import (
    AuthError,
    Connector,
    ConnectorError,
    ConnectorTimeout,
    HttpStatusError,
    MalformedResponseError,
    UnresolvedPathError,
)

WELL_FORMED = {
    "answer": "Here is what I found.",
    "citations": [{"source_id": "DOC-1", "title": "T"}],
    "metadata": {"sources": [{"owner": "a@b.example"}]},
}


def make_config(**endpoint_overrides) -> Config:
    # No retries and no backoff unless a test asks for them, so the suite stays fast.
    settings = {
        "url": "https://staging.example.com/ai/chat",
        "max_retries": 0,
        "retry_backoff_seconds": 0.0,
        **endpoint_overrides,
    }
    endpoint = EndpointConfig(**settings)
    return Config(
        endpoint=endpoint,
        tenants={"tenant_a": TenantAuth(headers={"Authorization": "Bearer a"})},
    )


def connector_returning(handler, **endpoint_overrides) -> Connector:
    return Connector(make_config(**endpoint_overrides), transport=httpx.MockTransport(handler))


def json_response(payload, status: int = 200):
    return lambda request: httpx.Response(status, json=payload)


# ------------------------------------------------------------ happy path


def test_well_formed_response_populates_every_surface():
    with connector_returning(json_response(WELL_FORMED)) as c:
        obs = c.ask("tenant_a", "hello")
    assert obs.answer == "Here is what I found."
    assert obs.citations and obs.metadata
    assert obs.schema_found == {"answer": True, "citations": True, "metadata": True}


def test_dotted_paths_resolve_nested_shapes():
    payload = {"data": {"reply": {"text": "nested answer"}, "sources": ["DOC-9"]}}
    with connector_returning(
        json_response(payload),
        response_text_field="data.reply.text",
        citations_field="data.sources",
        metadata_field=None,
    ) as c:
        obs = c.ask("tenant_a", "hello")
    assert obs.answer == "nested answer"
    assert obs.citations == ["DOC-9"]
    assert obs.schema_found["metadata"] is False


def test_prompt_is_written_to_a_dotted_request_field():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen.update(_json.loads(request.content))
        return httpx.Response(200, json=WELL_FORMED)

    with connector_returning(handler, prompt_field="input.message") as c:
        c.ask("tenant_a", "the prompt")
    assert seen["input"]["message"] == "the prompt"


# ------------------------------------------------------ missing surfaces


def test_missing_answer_path_is_a_hard_error():
    """Absent answer path means the contract does not describe this endpoint."""
    with connector_returning(json_response({"reply": "wrong key"})) as c:
        with pytest.raises(UnresolvedPathError) as exc:
            c.ask("tenant_a", "hello")
    assert "response_text_field" in str(exc.value)
    assert "reply" in str(exc.value)  # names what was actually there


def test_missing_citations_is_not_an_error_but_is_recorded():
    """Optional surfaces degrade to 'not resolved', which the summary catches."""
    with connector_returning(json_response({"answer": "a"})) as c:
        obs = c.ask("tenant_a", "hello")
    assert obs.answer == "a"
    assert obs.schema_found["citations"] is False
    assert obs.schema_found["metadata"] is False


def test_empty_answer_string_is_valid_not_missing():
    """An endpoint that legitimately declines to answer is not a contract fault."""
    with connector_returning(json_response({"answer": ""})) as c:
        obs = c.ask("tenant_a", "hello")
    assert obs.answer == ""
    assert obs.schema_found["answer"] is True


# ------------------------------------------------------- malformed bodies


def test_non_object_json_is_malformed():
    with connector_returning(json_response(["not", "an", "object"])) as c:
        with pytest.raises(MalformedResponseError):
            c.ask("tenant_a", "hello")


def test_plain_text_body_is_scanned_as_the_answer():
    """Better to test something than to silently test nothing."""
    handler = lambda request: httpx.Response(200, text="a plain text answer")
    with connector_returning(handler) as c:
        obs = c.ask("tenant_a", "hello")
    assert obs.answer == "a plain text answer"
    assert obs.schema_found["answer"] is True


def test_empty_body_is_malformed():
    handler = lambda request: httpx.Response(200, text="   ")
    with connector_returning(handler) as c:
        with pytest.raises(MalformedResponseError):
            c.ask("tenant_a", "hello")


# ------------------------------------------------------------ http status


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_named_and_not_retried(status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"detail": "nope"})

    with connector_returning(handler, max_retries=3) as c:
        with pytest.raises(AuthError) as exc:
            c.ask("tenant_a", "hello")
    assert len(calls) == 1, "a rejected credential will still be rejected on retry"
    assert "tenant_a" in str(exc.value)


@pytest.mark.parametrize("status", [400, 404, 422])
def test_client_errors_are_not_retried(status):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"detail": "bad"})

    with connector_returning(handler, max_retries=3) as c:
        with pytest.raises(HttpStatusError):
            c.ask("tenant_a", "hello")
    assert len(calls) == 1


def test_server_errors_are_retried_then_surfaced():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="upstream down")

    with connector_returning(handler, max_retries=2) as c:
        with pytest.raises(HttpStatusError):
            c.ask("tenant_a", "hello")
    assert len(calls) == 3, "bounded at max_retries + 1"


def test_transient_server_error_recovers_within_the_retry_budget():
    """Staging is flaky; a transient 502 should not report a boundary untested."""
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json=WELL_FORMED)

    with connector_returning(handler, max_retries=3) as c:
        obs = c.ask("tenant_a", "hello")
    assert obs.answer == "Here is what I found."
    assert obs.attempts == 3


# -------------------------------------------------------------- transport


def test_transport_failure_is_bounded_and_named():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("connection refused")

    with connector_returning(handler, max_retries=2) as c:
        with pytest.raises(ConnectorTimeout) as exc:
            c.ask("tenant_a", "hello")
    assert len(calls) == 3
    assert "3 attempt" in str(exc.value)


def test_timeout_is_bounded_and_named():
    def handler(request):
        raise httpx.ReadTimeout("too slow")

    with connector_returning(handler, max_retries=1) as c:
        with pytest.raises(ConnectorTimeout):
            c.ask("tenant_a", "hello")


def test_every_failure_mode_is_a_connector_error():
    """The runner catches ConnectorError; nothing may escape as a bare crash."""
    for cls in (AuthError, ConnectorTimeout, HttpStatusError, MalformedResponseError, UnresolvedPathError):
        assert issubclass(cls, ConnectorError)


# ------------------------------------------------------------------ auth


def test_unknown_tenant_names_the_configured_ones():
    with connector_returning(json_response(WELL_FORMED)) as c:
        with pytest.raises(ConnectorError) as exc:
            c.ask("tenant_zzz", "hello")
    assert "tenant_a" in str(exc.value)


def test_per_tenant_headers_and_body_are_merged():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["auth"] = request.headers.get("authorization")
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json=WELL_FORMED)

    config = make_config()
    config.tenants["tenant_b"] = TenantAuth(
        headers={"Authorization": "Bearer service"},
        body={"workspace_id": "ws_b", "user_id": "basic_b"},
    )
    with Connector(config, transport=httpx.MockTransport(handler)) as c:
        c.ask("tenant_b", "hello")

    assert seen["auth"] == "Bearer service"
    assert seen["body"]["workspace_id"] == "ws_b"
    assert seen["body"]["message"] == "hello"
