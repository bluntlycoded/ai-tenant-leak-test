"""Run-summary and ingest-stamp tests.

The property under test throughout: an under-scoped run must never be able to
look like a pass. That is the failure mode the whole product exists to prevent.
"""

from __future__ import annotations

import json

import pytest

from aitlt import stamp
from aitlt.models import Marker, MarkerKind, Match, Observation, TestCase, TestResult, TestRun

FULL_CONTRACT = {"answer": "answer", "citations": "citations", "metadata": "metadata"}
ALL_RESOLVED = {"answer": True, "citations": True, "metadata": True}


def make_run(results: list[TestResult]) -> TestRun:
    return TestRun(run_id="r1", environment="staging", results=results)


def case(cid: str = "t-01", category: str = "direct_retrieval") -> TestCase:
    return TestCase(id=cid, name="n", category=category, as_tenant="tenant_a", prompt="p")


def passing(cid: str = "t-01") -> TestResult:
    return TestResult(test_case=case(cid), status="pass", observation=Observation(answer="clean"))


def failing(cid: str = "t-02", kind: MarkerKind = MarkerKind.CANARY) -> TestResult:
    match = Match(
        marker=Marker(value="CANARY-X-1234567", kind=kind, tenant_id="tenant_b", document_id="D1"),
        location="answer",
        excerpt="...",
    )
    return TestResult(test_case=case(cid), status="fail", matches=[match])


def summarise(run: TestRun, *, resolved=None, configured=None, verified=True):
    return run.build_summary(
        surfaces_resolved=resolved if resolved is not None else ALL_RESOLVED,
        surfaces_configured=configured if configured is not None else FULL_CONTRACT,
        ingest_verified=verified,
        ingest_verified_at=None,
    )


def test_clean_run_is_a_pass():
    s = summarise(make_run([passing()]))
    assert (s.verdict, s.run_complete, s.contract_matched) == ("pass", True, True)


def test_leak_is_a_fail_with_counts():
    s = summarise(make_run([passing(), failing()]))
    assert s.verdict == "fail"
    assert (s.tests_failed, s.critical_findings) == (1, 1)


def test_unresolved_surface_makes_the_run_incomplete_not_a_pass():
    """The central guard: no findings plus a missing surface is NOT a pass."""
    s = summarise(make_run([passing()]), resolved={"answer": True, "citations": False, "metadata": True})
    assert s.verdict == "incomplete"
    assert s.contract_matched is False
    assert s.run_complete is False
    assert any("never resolved" in item for item in s.scope_not_tested)


def test_surface_set_to_null_is_out_of_scope_not_a_contract_breach():
    """Explicitly declining a surface is honest; silently missing one is not."""
    s = summarise(
        make_run([passing()]),
        resolved={"answer": True, "citations": False, "metadata": False},
        configured={"answer": "answer", "citations": None, "metadata": None},
    )
    assert s.verdict == "pass"
    assert s.contract_matched is True
    assert any("not configured" in item for item in s.scope_not_tested)
    assert "citations and source identifiers" not in s.scope_tested


def test_errored_test_makes_the_run_incomplete():
    errored = TestResult(test_case=case("t-03"), status="error", error="timeout")
    s = summarise(make_run([passing(), errored]))
    assert s.verdict == "incomplete"
    assert s.tests_errored == 1


def test_scope_tested_lists_only_resolved_surfaces():
    s = summarise(make_run([passing()]), resolved={"answer": True, "citations": True, "metadata": False})
    assert "assistant answer text" in s.scope_tested
    assert "client-visible document metadata" not in s.scope_tested


def test_not_tested_list_is_always_present():
    s = summarise(make_run([passing()]))
    assert any("reranker" in item for item in s.scope_not_tested)
    assert any("tool-call" in item for item in s.scope_not_tested)


def test_summary_serialises_first_in_json():
    run = make_run([passing()])
    run.summary = summarise(run)
    assert next(iter(json.loads(run.model_dump_json()))) == "summary"


# --------------------------------------------------------------------- stamp


@pytest.fixture
def fixtures_file(tmp_path):
    p = tmp_path / "fixtures.json"
    p.write_text('{"tenants": []}')
    return p


def test_stamp_absent_before_verification(fixtures_file):
    valid, _, reason = stamp.read(fixtures_file, "https://x/chat")
    assert valid is False
    assert "has not been run" in reason


def test_stamp_valid_after_write(fixtures_file):
    stamp.write(fixtures_file, "https://x/chat")
    valid, when, _ = stamp.read(fixtures_file, "https://x/chat")
    assert valid is True and when is not None


def test_stamp_invalidated_when_fixtures_change(fixtures_file):
    """Regenerated canaries exist nowhere in staging — every test would pass."""
    stamp.write(fixtures_file, "https://x/chat")
    fixtures_file.write_text('{"tenants": [{"changed": true}]}')
    valid, _, reason = stamp.read(fixtures_file, "https://x/chat")
    assert valid is False
    assert "fixtures changed" in reason


def test_stamp_invalidated_against_a_different_endpoint(fixtures_file):
    stamp.write(fixtures_file, "https://staging/chat")
    valid, _, reason = stamp.read(fixtures_file, "https://production/chat")
    assert valid is False
    assert "different endpoint" in reason
