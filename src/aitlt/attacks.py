"""Suite loading and the run loop."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from . import stamp
from .config import Config
from .connector import Connector, ConnectorError
from .detector import scan
from .models import FixtureSet, TestCase, TestResult, TestRun


class SuiteError(Exception):
    pass


def load_suite(path: str | Path) -> tuple[str, list[TestCase]]:
    p = Path(path)
    if not p.exists():
        raise SuiteError(f"no suite at {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    tests = raw.get("tests") or []
    if not tests:
        raise SuiteError(f"suite {p} defines no tests")
    cases = [TestCase.model_validate(t) for t in tests]

    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise SuiteError(f"duplicate test id {case.id!r} in {p}")
        seen.add(case.id)
    return raw.get("name", p.stem), cases


def resolve_suite_path(name: str, search_dir: str | Path = "suites") -> Path:
    """Accept either a suite name or an explicit path."""
    candidate = Path(name)
    if candidate.exists():
        return candidate
    for suffix in (".yaml", ".yml"):
        guess = Path(search_dir) / f"{name}{suffix}"
        if guess.exists():
            return guess
    raise SuiteError(f"could not find suite {name!r} as a path or in {search_dir}/")


def validate_against_fixtures(cases: list[TestCase], fixtures: FixtureSet) -> None:
    """Fail fast if a suite references a tenant the fixtures do not define."""
    known = {t.tenant_id for t in fixtures.tenants}
    for case in cases:
        for tenant_id in filter(None, [case.as_tenant, case.prime.as_tenant if case.prime else None]):
            if tenant_id not in known:
                raise SuiteError(
                    f"test {case.id!r} references tenant {tenant_id!r}, "
                    f"which is not in the fixtures ({sorted(known)})"
                )


def run_suite(
    config: Config,
    fixtures: FixtureSet,
    cases: list[TestCase],
    suite_name: str,
    on_result: Callable[[TestResult], None] | None = None,
) -> TestRun:
    run = TestRun(
        run_id=f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}",
        environment=config.environment,
        build_id=config.build_id,
        suite=suite_name,
        endpoint=config.endpoint.url,
    )

    # Union across the whole run: a field may legitimately be absent from one
    # response and present in another, so one miss is not a contract failure.
    surfaces_resolved = {"answer": False, "citations": False, "metadata": False}

    with Connector(config) as connector:
        for case in cases:
            result = _run_case(connector, fixtures, case)
            for key, present in result.observation.schema_found.items():
                surfaces_resolved[key] = surfaces_resolved.get(key, False) or present
            run.results.append(result)
            if on_result:
                on_result(result)

    run.finished_at = datetime.now(timezone.utc)

    ep = config.endpoint
    verified, verified_at, _ = stamp.read(config.fixtures_path, ep.url)
    run.summary = run.build_summary(
        surfaces_resolved=surfaces_resolved,
        surfaces_configured={
            "answer": ep.response_text_field,
            "citations": ep.citations_field,
            "metadata": ep.metadata_field,
        },
        ingest_verified=verified,
        ingest_verified_at=verified_at,
    )
    return run


def _run_case(connector: Connector, fixtures: FixtureSet, case: TestCase) -> TestResult:
    try:
        # Cache tests only: warm the target as the other tenant first.
        if case.prime:
            connector.ask(case.prime.as_tenant, case.prime.prompt)

        observation = connector.ask(case.as_tenant, case.prompt)
    except ConnectorError as exc:
        return TestResult(test_case=case, status="error", error=str(exc))

    forbidden = fixtures.forbidden_for(case.as_tenant)
    matches = scan(observation, forbidden)
    return TestResult(
        test_case=case,
        status="fail" if matches else "pass",
        matches=matches,
        observation=observation,
    )
