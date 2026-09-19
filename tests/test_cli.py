"""CLI exit-code and waiver tests.

These encode the commercial semantics of the tool, not just its plumbing. The
exit code is what a customer's pipeline gates on and what the verdict in the
report has to agree with, so each state is pinned here rather than proven by
reading the source.

Runs against a mock transport through a patched Connector, so no live server.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from aitlt import cli as cli_mod
from aitlt import connector as connector_mod
from aitlt import fixtures as fixtures_mod
from aitlt import stamp

runner = CliRunner()

CI_MARKERS = ["CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE", "TEAMCITY_VERSION"]
ENDPOINT = "https://staging.example.com/ai/chat"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Tests must not inherit the CI markers of the machine running them."""
    for name in CI_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TENANT_A_TOKEN", "a")
    monkeypatch.setenv("TENANT_B_TOKEN", "b")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A project directory with fixtures and a config, cwd'd into."""
    monkeypatch.chdir(tmp_path)
    fs = fixtures_mod.default_fixture_set()
    fixtures_mod.save(fs, tmp_path / "fixtures" / "fixtures.json")
    config = yaml.safe_load(cli_mod.DEFAULT_CONFIG)
    config["endpoint"]["url"] = ENDPOINT
    (tmp_path / "aitenant.yaml").write_text(yaml.safe_dump(config))
    # The suite lives alongside the package, not the cwd.
    suite = Path(__file__).resolve().parent.parent / "suites" / "quick-leak-check.yaml"
    return tmp_path, fs, str(suite)


def install_transport(monkeypatch, handler):
    """Make every Connector built by the CLI use this mock transport."""
    original = connector_mod.Connector.__init__

    def patched(self, config, transport=None):
        original(self, config, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(connector_mod.Connector, "__init__", patched)


def responder(fs, *, leak: bool = False, drop_citations: bool = False, leak_metadata_only: bool = False):
    """Fake target. Answers with the caller's own docs, or with everyone's.

    `leak_metadata_only` leaks just owners and projects — medium severity — so a
    run can have real findings that sit below a critical threshold.
    """
    tokens = {"Bearer a": "tenant_a", "Bearer b": "tenant_b"}
    all_docs = [d for t in fs.tenants for d in t.documents]

    def handler(request: httpx.Request) -> httpx.Response:
        tenant = tokens.get(request.headers.get("authorization", ""), "tenant_a")
        own = [d for d in all_docs if d.tenant_id == tenant]
        docs = all_docs if leak else own
        payload = {
            "answer": "\n".join(f"From '{d.title}': {d.body}" for d in docs),
            "metadata": {
                "sources": [
                    {"owner": d.metadata.get("owner", ""), "project": d.metadata.get("project", "")}
                    for d in (all_docs if (leak or leak_metadata_only) else own)
                ]
            },
        }
        if not drop_citations:
            payload["citations"] = [{"source_id": d.document_id, "title": d.title} for d in docs]
        return httpx.Response(200, json=payload)

    return handler


def run_test_cmd(*args, suite: str):
    return runner.invoke(cli_mod.app, ["test", "--suite", suite, *args])


def verdict_of(tmp_path) -> str:
    reports = sorted((tmp_path / "output").glob("*.report.json"))
    return json.loads(reports[-1].read_text())["summary"]["verdict"]


# ------------------------------------------------------------- exit codes


def test_clean_and_verified_exits_zero(workspace, monkeypatch):
    tmp_path, fs, suite = workspace
    install_transport(monkeypatch, responder(fs))
    stamp.write(tmp_path / "fixtures" / "fixtures.json", ENDPOINT)

    result = run_test_cmd(suite=suite)
    assert result.exit_code == 0
    assert verdict_of(tmp_path) == "pass"


def test_leak_above_threshold_exits_two(workspace, monkeypatch):
    tmp_path, fs, suite = workspace
    install_transport(monkeypatch, responder(fs, leak=True))
    stamp.write(tmp_path / "fixtures" / "fixtures.json", ENDPOINT)

    result = run_test_cmd("--fail-on", "critical", suite=suite)
    assert result.exit_code == 2
    assert verdict_of(tmp_path) == "fail"


def test_findings_below_threshold_exit_zero_but_say_so_loudly(workspace, monkeypatch):
    """A green shell on real findings is the most dangerous state in the tool.

    Only metadata leaks here, which are medium, so a critical threshold lets the
    build pass. The output must make it impossible to skim that as "nothing
    found".
    """
    tmp_path, fs, suite = workspace
    install_transport(monkeypatch, responder(fs, leak_metadata_only=True))
    stamp.write(tmp_path / "fixtures" / "fixtures.json", ENDPOINT)

    result = run_test_cmd("--fail-on", "critical", suite=suite)
    assert result.exit_code == 0
    assert verdict_of(tmp_path) == "fail", "the verdict is fail even though the shell is green"
    assert "LEAKS FOUND BELOW THRESHOLD" in result.output
    assert "These are real leaks" in result.output
    assert "medium" in result.output


def test_unresolved_configured_surface_exits_one(workspace, monkeypatch):
    tmp_path, fs, suite = workspace
    install_transport(monkeypatch, responder(fs, drop_citations=True))
    stamp.write(tmp_path / "fixtures" / "fixtures.json", ENDPOINT)

    result = run_test_cmd(suite=suite)
    assert result.exit_code == 1
    assert verdict_of(tmp_path) == "incomplete"
    assert "contract" in result.output.lower()


def test_missing_ingest_stamp_exits_one(workspace, monkeypatch):
    tmp_path, fs, suite = workspace
    install_transport(monkeypatch, responder(fs))

    result = run_test_cmd(suite=suite)
    assert result.exit_code == 1
    assert verdict_of(tmp_path) == "incomplete"


def test_stale_ingest_stamp_exits_one(workspace, monkeypatch):
    """Fixtures regenerated after verification: canaries no longer in the index."""
    tmp_path, fs, suite = workspace
    fixtures_path = tmp_path / "fixtures" / "fixtures.json"
    stamp.write(fixtures_path, ENDPOINT)
    fixtures_mod.save(fixtures_mod.default_fixture_set(), fixtures_path)  # fresh canaries
    install_transport(monkeypatch, responder(fs))

    result = run_test_cmd(suite=suite)
    assert result.exit_code == 1
    assert verdict_of(tmp_path) == "incomplete"
    assert "fixtures changed" in result.output


# ----------------------------------------------------------------- waiver


def test_waiver_locally_exits_zero_but_verdict_stays_incomplete(workspace, monkeypatch):
    tmp_path, fs, suite = workspace
    install_transport(monkeypatch, responder(fs))

    result = run_test_cmd("--allow-unverified-ingest", suite=suite)
    assert result.exit_code == 0, "the operator asked for this; do not fail their shell"
    assert verdict_of(tmp_path) == "incomplete", "but it is still not evidence"
    assert "not evidence" in result.output.lower()


@pytest.mark.parametrize("marker", CI_MARKERS)
def test_waiver_is_refused_in_ci(workspace, monkeypatch, marker):
    _, fs, suite = workspace
    install_transport(monkeypatch, responder(fs))
    monkeypatch.setenv(marker, "true")

    result = run_test_cmd("--allow-unverified-ingest", suite=suite)
    assert result.exit_code == 1
    assert "refused in CI" in result.output


def test_waiver_does_not_suppress_a_real_leak(workspace, monkeypatch):
    """A waived run still fails the shell when it finds something."""
    tmp_path, fs, suite = workspace
    install_transport(monkeypatch, responder(fs, leak=True))

    result = run_test_cmd("--allow-unverified-ingest", "--fail-on", "critical", suite=suite)
    assert result.exit_code == 2
    assert verdict_of(tmp_path) == "fail"


# ------------------------------------------------------- operational fails


def test_auth_failure_aborts_early_rather_than_grinding_through(workspace, monkeypatch):
    """A misfit endpoint should cost a few requests to discover, not the suite."""
    tmp_path, fs, suite = workspace
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"detail": "bad token"})

    install_transport(monkeypatch, handler)
    stamp.write(tmp_path / "fixtures" / "fixtures.json", ENDPOINT)

    result = run_test_cmd(suite=suite)
    assert result.exit_code == 1
    assert len(calls) <= 5, f"aborted after {len(calls)} requests, not the full suite"
    assert verdict_of(tmp_path) == "incomplete"


def test_missing_config_exits_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli_mod.app, ["test"])
    assert result.exit_code == 1
    assert "init" in result.output


def test_missing_fixtures_exits_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "aitenant.yaml").write_text(cli_mod.DEFAULT_CONFIG)
    result = runner.invoke(cli_mod.app, ["test"])
    assert result.exit_code == 1
