"""SSE parsing, aggregation, and the connector's streaming mode.

The property that matters: a leak in a streamed answer is the same leak. Once
reassembled, detection must be indistinguishable from the non-streaming path —
including when tokenisation splits a canary across frames, which is the way
streaming could quietly defeat an exact-match detector.
"""

from __future__ import annotations

import json

import httpx
import pytest

from aitlt import sse
from aitlt.config import Config, EndpointConfig, StreamingConfig, TenantAuth
from aitlt.connector import Connector, MalformedResponseError, UnresolvedPathError, UnsupportedResponseError
from aitlt.detector import scan
from aitlt.models import Marker, MarkerKind, Observation

CANARY = "CANARY-UMBRA-PRICING-9BE9"


def sse_body(*frames: str) -> str:
    return "".join(f"data: {f}\n\n" for f in frames)


def deltas(text: str, size: int = 5) -> str:
    """Chop text into delta frames, deliberately mid-token."""
    parts = [json.dumps({"delta": text[i : i + size]}) for i in range(0, len(text), size)]
    return sse_body(*parts, "[DONE]")


# ------------------------------------------------------------- frame parsing


def test_frames_are_split_on_blank_lines():
    assert list(sse.iter_frames("data: one\n\ndata: two\n\n")) == ["one", "two"]


def test_multi_line_data_is_joined_with_newlines():
    assert list(sse.iter_frames("data: line1\ndata: line2\n\n")) == ["line1\nline2"]


def test_comments_and_other_fields_are_ignored():
    body = ": keep-alive\nevent: message\nid: 7\nretry: 100\ndata: payload\n\n"
    assert list(sse.iter_frames(body)) == ["payload"]


def test_crlf_and_missing_trailing_blank_line_are_tolerated():
    assert list(sse.iter_frames("data: a\r\n\r\ndata: b")) == ["a", "b"]


def test_leading_space_after_colon_is_stripped_once():
    assert list(sse.iter_frames("data:  two spaces\n\n")) == [" two spaces"]


# --------------------------------------------------------------- aggregation


def test_deltas_are_concatenated_in_order():
    agg = sse.aggregate(deltas("hello world"), delta_field="delta")
    assert agg.answer == "hello world"
    assert agg.answer_found is True


def test_done_sentinel_stops_aggregation():
    body = sse_body(json.dumps({"delta": "kept"}), "[DONE]", json.dumps({"delta": "ignored"}))
    assert sse.aggregate(body, delta_field="delta").answer == "kept"


def test_openai_shaped_delta_path_resolves():
    frames = [json.dumps({"choices": [{"delta": {"content": c}}]}) for c in "abc"]
    agg = sse.aggregate(sse_body(*frames), delta_field="choices.0.delta.content")
    assert agg.answer == "abc"


def test_final_frame_supplies_citations_and_metadata():
    body = sse_body(
        json.dumps({"delta": "text"}),
        json.dumps({"citations": [{"source_id": "DOC-1"}], "metadata": {"sources": []}}),
        "[DONE]",
    )
    agg = sse.aggregate(
        body, delta_field="delta", citations_field="citations", metadata_field="metadata"
    )
    assert agg.answer == "text"
    assert agg.citations == [{"source_id": "DOC-1"}]
    assert agg.metadata == {"sources": []}


def test_whole_message_is_used_when_no_deltas_arrive():
    body = sse_body(json.dumps({"answer": "complete message"}), "[DONE]")
    agg = sse.aggregate(body, delta_field="delta", text_field="answer")
    assert agg.answer == "complete message"


def test_deltas_win_over_a_final_whole_message():
    """A truncated stream must read as truncated, not be silently repaired."""
    body = sse_body(json.dumps({"delta": "par"}), json.dumps({"answer": "PARTIAL REPAIRED"}))
    agg = sse.aggregate(body, delta_field="delta", text_field="answer")
    assert agg.answer == "par"


def test_unparseable_frames_are_counted_not_hidden():
    body = sse_body("not json", json.dumps({"delta": "ok"}), "[[[")
    agg = sse.aggregate(body, delta_field="delta")
    assert agg.answer == "ok"
    assert agg.unparseable_frames == 2


def test_no_resolvable_delta_reports_not_found():
    agg = sse.aggregate(sse_body(json.dumps({"token": "x"})), delta_field="delta")
    assert agg.answer_found is False


# ----------------------------------------------------- the detection property


def test_a_canary_split_across_frames_is_still_detected():
    """Tokenisation must not be able to hide an exact-match marker."""
    body = deltas(f"The reference is {CANARY} and that is all.", size=3)
    agg = sse.aggregate(body, delta_field="delta")

    matches = scan(
        Observation(answer=agg.answer),
        [Marker(value=CANARY, kind=MarkerKind.CANARY, tenant_id="tenant_b", document_id="D1")],
    )
    assert len(matches) == 1, "reassembly must restore the marker"


# --------------------------------------------------------- connector wiring


def make_connector(handler, streaming: StreamingConfig | None = None, **overrides) -> Connector:
    endpoint = EndpointConfig(
        url="https://staging.example.com/ai/chat",
        max_retries=0,
        retry_backoff_seconds=0.0,
        streaming=streaming or StreamingConfig(),
        **overrides,
    )
    config = Config(endpoint=endpoint, tenants={"tenant_a": TenantAuth(headers={"Authorization": "Bearer a"})})
    return Connector(config, transport=httpx.MockTransport(handler))


def sse_handler(body: str):
    return lambda request: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def test_streaming_mode_off_still_refuses_sse():
    """Opt-in only. Silence is not consent to scan a blob."""
    with make_connector(sse_handler(deltas("hi"))) as c:
        with pytest.raises(UnsupportedResponseError) as exc:
            c.ask("tenant_a", "hello")
    assert "streaming.mode: sse" in str(exc.value)


def test_streaming_mode_on_reassembles_the_answer():
    body = sse_body(
        *[json.dumps({"delta": c}) for c in "leaked "],
        json.dumps({"delta": CANARY}),
        json.dumps({"citations": [{"source_id": "DOC-B-1"}], "metadata": {"owner": "x@y.example"}}),
        "[DONE]",
    )
    with make_connector(sse_handler(body), StreamingConfig(mode="sse")) as c:
        obs = c.ask("tenant_a", "hello")

    assert obs.answer == f"leaked {CANARY}"
    assert obs.citations == ["source_id=DOC-B-1"]
    assert obs.metadata == {"owner": "x@y.example"}
    assert obs.schema_found == {"answer": True, "citations": True, "metadata": True}


def test_streaming_mode_on_still_handles_a_plain_json_response():
    """Some endpoints stream on success and return JSON on error."""
    handler = lambda request: httpx.Response(200, json={"answer": "plain"})
    with make_connector(handler, StreamingConfig(mode="sse")) as c:
        assert c.ask("tenant_a", "hello").answer == "plain"


def test_wrong_delta_field_names_the_keys_it_actually_saw():
    body = sse_body(json.dumps({"token": "x", "index": 0}), "[DONE]")
    with make_connector(sse_handler(body), StreamingConfig(mode="sse")) as c:
        with pytest.raises(UnresolvedPathError) as exc:
            c.ask("tenant_a", "hello")
    assert "delta_field" in str(exc.value)
    assert "token" in str(exc.value)


def test_empty_stream_is_malformed():
    with make_connector(sse_handler(""), StreamingConfig(mode="sse")) as c:
        with pytest.raises(MalformedResponseError):
            c.ask("tenant_a", "hello")


def test_ndjson_is_still_refused_even_in_sse_mode():
    """The narrow mode is SSE. NDJSON remains out of scope."""
    handler = lambda request: httpx.Response(
        200, text='{"delta":"a"}\n', headers={"content-type": "application/x-ndjson"}
    )
    with make_connector(handler, StreamingConfig(mode="sse")) as c:
        with pytest.raises(UnsupportedResponseError):
            c.ask("tenant_a", "hello")
