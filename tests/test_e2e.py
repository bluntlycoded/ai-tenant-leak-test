"""End-to-end lifecycle tests against a live dev target.

`calibrate.sh` proves the detector fires on each leak path. These prove the
whole operator journey works over a real socket: init, seed, verify, test, and
the two ways a run stops being trustworthy afterwards.

Runs a real uvicorn process rather than an in-process ASGI transport, because
the thing being proven is that the product works as sold, not that the functions
compose.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

from aitlt import cli as cli_mod
from aitlt import fixtures as fixtures_mod

REPO = Path(__file__).resolve().parent.parent
SUITE = str(REPO / "suites" / "quick-leak-check.yaml")

pytestmark = pytest.mark.e2e


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Target:
    """A live devtarget process with a chosen set of leak flags."""

    def __init__(self, fixtures_path: Path, **leak_flags: str):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        env = {
            **os.environ,
            "AITLT_FIXTURES": str(fixtures_path),
            # Never inherit the developer's flags.
            **{
                name: ""
                for name in (
                    "LEAK_POST_FILTER",
                    "LEAK_CITATION_RESOLVER",
                    "LEAK_METADATA",
                    "LEAK_SEMANTIC_CACHE",
                    "LEAK_INDIRECT_INJECTION",
                )
            },
            **leak_flags,
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "devtarget.app:app", "--port", str(self.port), "--log-level", "error"],
            cwd=REPO,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_ready()

    def _wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("devtarget exited during startup")
            try:
                if httpx.get(f"{self.url}/health", timeout=1.0).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError("devtarget did not become ready")

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A seeded project directory, cwd'd into, with tokens exported."""
    monkeypatch.chdir(tmp_path)
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE", "TEAMCITY_VERSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TENANT_A_TOKEN", "tenant-a-token")
    monkeypatch.setenv("TENANT_B_TOKEN", "tenant-b-token")
    fixtures_path = tmp_path / "fixtures" / "fixtures.json"
    fixtures_mod.save(fixtures_mod.default_fixture_set(), fixtures_path)
    return tmp_path, fixtures_path


def write_config(tmp_path: Path, url: str, **endpoint_overrides) -> Path:
    config = yaml.safe_load(cli_mod.DEFAULT_CONFIG)
    config["endpoint"]["url"] = f"{url}/ai/chat"
    config["endpoint"].update(endpoint_overrides)
    path = tmp_path / "aitenant.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def aitenant(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "aitlt.cli", *args],
        capture_output=True,
        text=True,
    )


def latest_summary(tmp_path: Path) -> dict:
    reports = sorted((tmp_path / "output").glob("*.report.json"))
    return json.loads(reports[-1].read_text())["summary"]


# ---------------------------------------------------------------- lifecycle


def test_isolated_target_verified_ingest_clean_pass(project):
    tmp_path, fixtures_path = project
    target = Target(fixtures_path)
    try:
        write_config(tmp_path, target.url)
        assert aitenant("verify-ingest").returncode == 0

        result = aitenant("test", "--suite", SUITE, "--build", "e2e")
        assert result.returncode == 0, result.stdout + result.stderr

        summary = latest_summary(tmp_path)
        assert summary["verdict"] == "pass"
        assert summary["run_complete"] is True
        assert summary["ingest_verified"] is True
        assert summary["tests_failed"] == 0
    finally:
        target.stop()


def test_leaking_target_verified_ingest_reports_fail_with_evidence(project):
    tmp_path, fixtures_path = project
    target = Target(fixtures_path, LEAK_POST_FILTER="1")
    try:
        write_config(tmp_path, target.url)
        assert aitenant("verify-ingest").returncode == 0

        result = aitenant("test", "--suite", SUITE, "--fail-on", "critical")
        assert result.returncode == 2, result.stdout

        summary = latest_summary(tmp_path)
        assert summary["verdict"] == "fail"
        assert summary["run_complete"] is True
        assert summary["critical_findings"] > 0

        # The evidence a customer engineer reproduces from must be present.
        report = sorted((tmp_path / "output").glob("*.report.json"))[-1]
        failing = [r for r in json.loads(report.read_text())["results"] if r["status"] == "fail"]
        assert failing
        first = failing[0]
        assert first["test_case"]["prompt"]
        assert first["matches"][0]["marker"]["value"].startswith("CANARY-")
        assert first["observation"]["answer"]
    finally:
        target.stop()


def test_bad_connector_config_yields_incomplete_not_a_pass(project):
    """A response path that never resolves must not read as a clean run."""
    tmp_path, fixtures_path = project
    target = Target(fixtures_path)
    try:
        write_config(tmp_path, target.url, citations_field="data.sources")
        # verify-ingest catches it first, which is the point of running it.
        assert aitenant("verify-ingest").returncode == 1

        result = aitenant("test", "--suite", SUITE)
        assert result.returncode == 1, result.stdout
        assert latest_summary(tmp_path)["verdict"] == "incomplete"
        assert latest_summary(tmp_path)["contract_matched"] is False
    finally:
        target.stop()


def test_fixtures_changed_after_verification_yields_incomplete(project):
    """Regenerated canaries exist nowhere in the index; a pass would be fiction."""
    tmp_path, fixtures_path = project
    target = Target(fixtures_path)
    try:
        write_config(tmp_path, target.url)
        assert aitenant("verify-ingest").returncode == 0

        fixtures_mod.save(fixtures_mod.default_fixture_set(), fixtures_path)

        result = aitenant("test", "--suite", SUITE)
        assert result.returncode == 1, result.stdout
        assert "fixtures changed" in result.stdout
        assert latest_summary(tmp_path)["verdict"] == "incomplete"
    finally:
        target.stop()


def test_wrong_endpoint_after_verification_yields_incomplete(project):
    """A stamp taken against staging must not vouch for a different host."""
    tmp_path, fixtures_path = project
    target = Target(fixtures_path)
    try:
        write_config(tmp_path, target.url)
        assert aitenant("verify-ingest").returncode == 0

        # Same server, different spelling of the host: the stamp should not carry over.
        write_config(tmp_path, target.url.replace("127.0.0.1", "localhost"))

        result = aitenant("test", "--suite", SUITE)
        assert result.returncode == 1, result.stdout
        assert latest_summary(tmp_path)["verdict"] == "incomplete"
    finally:
        target.stop()


def test_sse_endpoint_detects_the_same_leak_as_the_json_endpoint(project):
    """The whole point of the streaming mode: same leak, different transport.

    The devtarget chops the answer into 7-character frames, so canaries are
    split mid-token and only survive if reassembly works.
    """
    tmp_path, fixtures_path = project
    target = Target(fixtures_path, LEAK_POST_FILTER="1")
    try:
        write_config(
            tmp_path,
            target.url,
            streaming={"mode": "sse", "delta_field": "delta", "done_sentinel": "[DONE]"},
        )
        # Point at the streaming route rather than the JSON one.
        config = yaml.safe_load((tmp_path / "aitenant.yaml").read_text())
        config["endpoint"]["url"] = f"{target.url}/ai/chat/stream"
        (tmp_path / "aitenant.yaml").write_text(yaml.safe_dump(config))

        assert aitenant("verify-ingest").returncode == 0, "canaries must survive tokenisation"

        result = aitenant("test", "--suite", SUITE, "--fail-on", "critical")
        assert result.returncode == 2, result.stdout

        summary = latest_summary(tmp_path)
        assert summary["verdict"] == "fail"
        assert summary["contract_matched"] is True, "citations and metadata arrive in the final frame"
        assert summary["critical_findings"] > 0
    finally:
        target.stop()


def test_sse_endpoint_is_refused_when_streaming_mode_is_off(project):
    """Opt-in only: pointing at a stream without enabling the mode must not pass."""
    tmp_path, fixtures_path = project
    target = Target(fixtures_path)
    try:
        write_config(tmp_path, target.url)
        config = yaml.safe_load((tmp_path / "aitenant.yaml").read_text())
        config["endpoint"]["url"] = f"{target.url}/ai/chat/stream"
        (tmp_path / "aitenant.yaml").write_text(yaml.safe_dump(config))

        result = aitenant("test", "--suite", SUITE, "--allow-unverified-ingest")
        assert result.returncode == 1
        assert latest_summary(tmp_path)["verdict"] == "incomplete"
    finally:
        target.stop()


def test_unreachable_endpoint_aborts_early(project):
    """A dead endpoint should cost a few requests, not the whole suite."""
    tmp_path, fixtures_path = project
    write_config(tmp_path, f"http://127.0.0.1:{free_port()}")

    started = time.time()
    result = aitenant("test", "--suite", SUITE, "--allow-unverified-ingest")
    elapsed = time.time() - started

    assert result.returncode == 1
    assert elapsed < 60, "aborted rather than retrying every test in the suite"
    summary = latest_summary(tmp_path)
    assert summary["verdict"] == "incomplete"
    assert summary["tests_errored"] == summary["tests_run"]
