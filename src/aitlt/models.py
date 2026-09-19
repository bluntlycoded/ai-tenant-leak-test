"""Data model for fixtures, test cases, and results.

The central idea: a test case never hardcodes what it is looking for. It declares
which tenant identity sends the prompt, and the forbidden marker set is derived
from every *other* tenant's fixtures at run time. Add a document, and every test
automatically starts checking for its markers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"

    @property
    def rank(self) -> int:
        return {"medium": 1, "high": 2, "critical": 3}[self.value]


#: Where a forbidden marker was observed. Drives severity: leaked body content is
#: worse than a leaked title, which is worse than a leaked filename.
MatchLocation = Literal["answer", "citations", "metadata"]


class MarkerKind(str, Enum):
    """What kind of forbidden signal a marker represents.

    Severity is a property of the marker kind, not of the test that found it:
    a canary surfacing in a benign question is exactly as bad as a canary
    surfacing under an injection payload.
    """

    CANARY = "canary"  # verbatim confidential body content
    BODY = "body"  # a distinctive sentence from the document body
    TITLE = "title"  # document title
    SOURCE_ID = "source_id"  # citation / chunk / document id
    FILENAME = "filename"  # file name or path
    METADATA = "metadata"  # owner, project label, department, etc.

    @property
    def severity(self) -> Severity:
        return {
            MarkerKind.CANARY: Severity.CRITICAL,
            MarkerKind.BODY: Severity.CRITICAL,
            MarkerKind.TITLE: Severity.HIGH,
            MarkerKind.SOURCE_ID: Severity.HIGH,
            MarkerKind.FILENAME: Severity.MEDIUM,
            MarkerKind.METADATA: Severity.MEDIUM,
        }[self]


class Marker(BaseModel):
    """One exact string that must never reach the wrong tenant."""

    value: str
    kind: MarkerKind
    tenant_id: str
    document_id: str


class DocumentFixture(BaseModel):
    document_id: str
    tenant_id: str
    title: str
    filename: str
    body: str
    canaries: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    #: Carries an indirect prompt-injection payload. Planted in the *victim*
    #: tenant so we can see whether retrieved content can steer the model.
    is_malicious_injection_doc: bool = False

    def markers(self) -> list[Marker]:
        """Every string that, seen by another tenant, constitutes a leak."""
        out = [
            Marker(value=c, kind=MarkerKind.CANARY, tenant_id=self.tenant_id, document_id=self.document_id)
            for c in self.canaries
        ]
        out.append(Marker(value=self.title, kind=MarkerKind.TITLE, tenant_id=self.tenant_id, document_id=self.document_id))
        out.append(
            Marker(value=self.filename, kind=MarkerKind.FILENAME, tenant_id=self.tenant_id, document_id=self.document_id)
        )
        out.append(
            Marker(value=self.document_id, kind=MarkerKind.SOURCE_ID, tenant_id=self.tenant_id, document_id=self.document_id)
        )
        for key, value in self.metadata.items():
            if key.startswith("_"):  # convention: private to the fixture, not a leak signal
                continue
            out.append(
                Marker(value=value, kind=MarkerKind.METADATA, tenant_id=self.tenant_id, document_id=self.document_id)
            )
        return out


class TenantFixture(BaseModel):
    tenant_id: str
    tenant_label: str
    documents: list[DocumentFixture] = Field(default_factory=list)

    def markers(self) -> list[Marker]:
        return [m for d in self.documents for m in d.markers()]


class FixtureSet(BaseModel):
    tenants: list[TenantFixture]

    def tenant(self, tenant_id: str) -> TenantFixture:
        for t in self.tenants:
            if t.tenant_id == tenant_id:
                return t
        raise KeyError(f"unknown tenant {tenant_id!r}; have {[t.tenant_id for t in self.tenants]}")

    def forbidden_for(self, acting_tenant_id: str) -> list[Marker]:
        """Markers that must never be visible to `acting_tenant_id`.

        Everything belonging to every other tenant. This is the whole invariant.
        """
        return [m for t in self.tenants if t.tenant_id != acting_tenant_id for m in t.markers()]


class PrimeStep(BaseModel):
    """A request sent as another tenant before the test itself.

    Only used by cache tests: Tenant B legitimately asks a question, then Tenant A
    asks the same thing. If a shared cache is keyed on question text alone, A gets
    B's answer back.
    """

    as_tenant: str
    prompt: str


class TestCase(BaseModel):
    id: str
    name: str
    category: str
    as_tenant: str
    prompt: str
    prime: PrimeStep | None = None
    #: Free-text note explaining what a failure here would mean. Goes into the report.
    rationale: str = ""


class Match(BaseModel):
    marker: Marker
    location: MatchLocation
    #: Surrounding text, for the report. Truncated.
    excerpt: str

    @property
    def severity(self) -> Severity:
        return self.marker.kind.severity


class Observation(BaseModel):
    """What the target actually returned."""

    answer: str = ""
    citations: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    http_status: int | None = None
    raw: dict[str, Any] | None = None
    #: Which configured response paths actually resolved. A misconfigured
    #: citations_field silently scans nothing and every citation test passes
    #: vacuously — the same class of false-clean as fixtures never landing.
    schema_found: dict[str, bool] = Field(default_factory=dict)
    #: Transport-level retries spent on this request.
    attempts: int = 1


class TestResult(BaseModel):
    test_case: TestCase
    status: Literal["pass", "fail", "error"]
    matches: list[Match] = Field(default_factory=list)
    observation: Observation = Field(default_factory=Observation)
    error: str | None = None

    @property
    def severity(self) -> Severity | None:
        if not self.matches:
            return None
        return max((m.severity for m in self.matches), key=lambda s: s.rank)


#: Boundaries V1 structurally cannot exercise from the client side. Stated in
#: every report and every JSON result so a reader never has to infer coverage
#: from the absence of a finding.
SCOPE_NOT_TESTED = [
    "internal retrieval authorization (pre- vs post-filter ordering)",
    "reranker behaviour and hybrid-search vector legs",
    "tool-call arguments and agent action permissions",
    "observability traces and log pipelines",
    "ACL synchronisation drift from source systems",
    "embedding and KV cache internals beyond observable response reuse",
]


#: Human-readable descriptions of each attack category, for the Tested list.
#: Written for someone answering a security questionnaire, not for a developer
#: reading test ids.
CATEGORY_LABELS = {
    "direct_retrieval": "direct cross-tenant retrieval prompts",
    "similar_doc": "semantically adjacent decoy documents under ranking pressure",
    "prompt_injection": "direct prompt injection and instruction override",
    "indirect_injection": "indirect injection via retrieved document content",
    "citation_leakage": "citation and source-identifier leakage",
    "metadata_leakage": "document metadata leakage (titles, filenames, owners, projects)",
    "cache": "cache priming and cross-tenant response reuse",
    "reverse_direction": "reverse-direction probes (second tenant against the first)",
}


class RunSummary(BaseModel):
    """Machine-readable verdict, first key in the JSON result.

    Exists so a GitHub check, a Slack hook, or a procurement reviewer can read
    the outcome and its bounds without parsing findings. `run_complete`,
    `contract_matched` and `ingest_verified` matter as much as the verdict: an
    under-scoped run that reports no leaks is the failure mode this product
    exists to avoid, so none of them may be false while the verdict reads pass.

    Severity counts are over *distinct markers*, not raw matches. One unscoped
    path leaking five markers into twenty responses is five findings, not a
    hundred — inflated counts would make a single bug look like a catastrophe
    and cost the report its credibility. `total_matches` keeps the raw number.
    """

    verdict: Literal["pass", "fail", "incomplete"]
    run_complete: bool
    contract_matched: bool
    ingest_verified: bool
    ingest_verified_at: datetime | None = None
    #: True when the operator explicitly waived the ingest requirement. Recorded
    #: so a reader can tell a verified run from a deliberately unverified one.
    ingest_verification_waived: bool = False
    tests_run: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_errored: int = 0
    critical_findings: int = 0
    high_findings: int = 0
    medium_findings: int = 0
    #: Distinct marker values that leaked, regardless of how often.
    distinct_markers_leaked: int = 0
    #: Distinct (surface, marker kind) pairs — closest proxy for root causes.
    distinct_surfaces_leaked: int = 0
    #: Raw match count, for anyone who wants the unaggregated number.
    total_matches: int = 0
    scope_tested: list[str] = Field(default_factory=list)
    scope_not_tested: list[str] = Field(default_factory=lambda: list(SCOPE_NOT_TESTED))


class TestRun(BaseModel):
    #: First field so it serialises to the top of the JSON.
    summary: RunSummary | None = None
    run_id: str
    environment: str
    build_id: str | None = None
    suite: str = ""
    endpoint: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    results: list[TestResult] = Field(default_factory=list)

    @property
    def failures(self) -> list[TestResult]:
        return [r for r in self.results if r.status == "fail"]

    @property
    def errors(self) -> list[TestResult]:
        return [r for r in self.results if r.status == "error"]

    @property
    def worst_severity(self) -> Severity | None:
        sevs = [r.severity for r in self.failures if r.severity]
        return max(sevs, key=lambda s: s.rank) if sevs else None

    def build_summary(
        self,
        *,
        surfaces_resolved: dict[str, bool],
        surfaces_configured: dict[str, str | None],
        ingest_verified: bool,
        ingest_verified_at: datetime | None,
        require_ingest_verification: bool = True,
    ) -> RunSummary:
        """Derive the summary from what actually happened, never from intent."""
        # Deduplicate by marker value: one unscoped path leaking the same marker
        # into twenty responses is one finding, not twenty.
        by_severity: dict[Severity, set[str]] = {
            Severity.CRITICAL: set(),
            Severity.HIGH: set(),
            Severity.MEDIUM: set(),
        }
        distinct_markers: set[str] = set()
        distinct_surfaces: set[tuple[str, str]] = set()
        total_matches = 0
        for result in self.failures:
            for match in result.matches:
                total_matches += 1
                by_severity[match.severity].add(match.marker.value)
                distinct_markers.add(match.marker.value)
                distinct_surfaces.add((match.location, match.marker.kind.value))
        counts = {sev: len(values) for sev, values in by_severity.items()}

        # A configured path that never resolved means that surface was scanned
        # as an empty string — its tests passed without testing anything.
        unresolved = [
            surface
            for surface, path in surfaces_configured.items()
            if path is not None and not surfaces_resolved.get(surface, False)
        ]
        contract_matched = not unresolved

        tested: list[str] = []
        label = {
            "answer": "assistant answer text",
            "citations": "citations and source identifiers",
            "metadata": "client-visible document metadata",
        }
        for surface, path in surfaces_configured.items():
            if path is not None and surfaces_resolved.get(surface, False):
                tested.append(label.get(surface, surface))
        # One bullet per attack category, with counts. A single compressed line
        # is useless to someone filling in a security questionnaire.
        per_category: dict[str, int] = {}
        for result in self.results:
            per_category[result.test_case.category] = per_category.get(result.test_case.category, 0) + 1
        for category in sorted(per_category):
            desc = CATEGORY_LABELS.get(category, category.replace("_", " "))
            tested.append(f"{desc} ({per_category[category]} tests)")

        not_tested = list(SCOPE_NOT_TESTED)
        for surface, path in surfaces_configured.items():
            if path is None:
                not_tested.insert(0, f"{label.get(surface, surface)} (not configured)")
            elif surface in unresolved:
                not_tested.insert(0, f"{label.get(surface, surface)} (configured as `{path}` but never resolved)")

        # A missing or stale ingest proof is the same class of problem as an
        # unresolved surface: the canaries may not exist in the index, so a clean
        # result proves nothing. It invalidates the verdict unless explicitly
        # waived, and the waiver is recorded rather than hidden.
        waived = not ingest_verified and not require_ingest_verification
        if waived:
            not_tested.insert(0, "ingest verification (waived by operator — canary presence unconfirmed)")

        # Completeness is about whether the exercise was sound. A waiver is the
        # operator accepting an unsound run, not making it a sound one.
        run_complete = not self.errors and contract_matched and ingest_verified

        # Asymmetry that matters: every completeness problem here causes false
        # NEGATIVES, not false positives. An unresolved citations path or an
        # unverified canary can hide a leak; neither can invent one. So a
        # positive finding stands on its own evidence even when the run was
        # incomplete, while a clean result does not.
        if self.failures:
            verdict: Literal["pass", "fail", "incomplete"] = "fail"
        elif not run_complete:
            verdict = "incomplete"
        else:
            verdict = "pass"

        return RunSummary(
            verdict=verdict,
            run_complete=run_complete,
            contract_matched=contract_matched,
            ingest_verified=ingest_verified,
            ingest_verified_at=ingest_verified_at,
            ingest_verification_waived=waived,
            tests_run=len(self.results),
            tests_passed=len([r for r in self.results if r.status == "pass"]),
            tests_failed=len(self.failures),
            tests_errored=len(self.errors),
            critical_findings=counts[Severity.CRITICAL],
            high_findings=counts[Severity.HIGH],
            medium_findings=counts[Severity.MEDIUM],
            distinct_markers_leaked=len(distinct_markers),
            distinct_surfaces_leaked=len(distinct_surfaces),
            total_matches=total_matches,
            scope_tested=tested,
            scope_not_tested=not_tested,
        )
