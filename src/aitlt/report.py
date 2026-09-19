"""Report generation.

The report is the product. Two constraints shape it:

  - a CTO should understand the verdict in one minute;
  - an engineer should reproduce a failure in five.

So every finding carries the literal prompt, the identity that sent it, the
exact marker that leaked, and where it appeared. The limitations section is not
boilerplate — output-level testing genuinely cannot prove internal retrieval
authorization, and the report says so rather than implying assurance it has not
earned.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import Severity, TestResult, TestRun

#: Maps a finding to the class of fix, not a specific patch. Naming the layer is
#: useful; pretending to know the customer's codebase is not.
REMEDIATION = {
    "answer": (
        "Apply the tenant scope filter *before* similarity search, not after. "
        "Confirm every retrieval path is covered — including the vector leg of hybrid "
        "search, any reranker that re-queries the index, and background or eval jobs."
    ),
    "citations": (
        "Scope the citation and chunk-id resolver with the same authorization check as "
        "retrieval. A source id that resolves to readable text for the wrong caller is an "
        "IDOR regardless of whether the body was filtered."
    ),
    "metadata": (
        "Filter document metadata (titles, filenames, owners, project labels) on the same "
        "boundary as content. Metadata alone discloses another customer's people and projects."
    ),
}

CATEGORY_NOTES = {
    "cache": (
        "Additionally: key any response or semantic cache on the tenant/authorization scope, "
        "not on question text alone."
    ),
    "indirect_injection": (
        "Additionally: treat retrieved document content as untrusted data, never as instructions, "
        "and constrain what the model is permitted to output after retrieval."
    ),
}

_SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM]


def _fmt_time(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "—"


def _remediation_for(result: TestResult) -> str:
    locations = {m.location for m in result.matches}
    parts = [REMEDIATION[loc] for loc in ("answer", "citations", "metadata") if loc in locations]
    note = CATEGORY_NOTES.get(result.test_case.category)
    if note:
        parts.append(note)
    return " ".join(parts)


def _truncate(text: str, limit: int = 1200) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "\n\n[... truncated, full response in the JSON report ...]"


def to_markdown(run: TestRun) -> str:
    failures = run.failures
    errors = run.errors
    passed = [r for r in run.results if r.status == "pass"]
    verdict = "FAIL" if failures else ("ERROR" if errors else "PASS")

    lines: list[str] = []
    lines.append("# AI Tenant Leak Test — Report")
    lines.append("")
    lines.append(f"**Result: {verdict}**")
    if failures:
        worst = run.worst_severity
        lines.append("")
        lines.append(
            f"{len(failures)} of {len(run.results)} tests leaked data across the tenant boundary. "
            f"Highest severity: **{worst.value if worst else 'unknown'}**."
        )
    elif errors:
        lines.append("")
        lines.append(f"{len(errors)} tests could not be executed. See Errors below.")
    else:
        lines.append("")
        lines.append(
            f"All {len(run.results)} tests passed. No forbidden marker from another tenant appeared "
            "in any answer, citation, or metadata field under the conditions tested."
        )
    lines.append("")

    lines.append("| | |")
    lines.append("|---|---|")
    lines.append(f"| Environment | {run.environment} |")
    lines.append(f"| Build | {run.build_id or 'not supplied'} |")
    lines.append(f"| Endpoint | `{run.endpoint}` |")
    lines.append(f"| Suite | {run.suite} |")
    lines.append(f"| Run ID | `{run.run_id}` |")
    lines.append(f"| Started | {_fmt_time(run.started_at)} |")
    lines.append(f"| Finished | {_fmt_time(run.finished_at)} |")
    lines.append(f"| Tests run | {len(run.results)} |")
    lines.append(f"| Passed | {len(passed)} |")
    lines.append(f"| Failed | {len(failures)} |")
    lines.append(f"| Errors | {len(errors)} |")
    lines.append("")

    if failures:
        lines.extend(_surface_summary(failures))
        lines.append("## Findings")
        lines.append("")
        ordered = sorted(
            failures,
            key=lambda r: (-(r.severity.rank if r.severity else 0), r.test_case.id),
        )
        for result in ordered:
            lines.extend(_finding_block(result))

    if errors:
        lines.append("## Errors")
        lines.append("")
        lines.append("These tests did not execute, so the boundary they cover was **not** tested.")
        lines.append("")
        for result in errors:
            lines.append(f"- `{result.test_case.id}` {result.test_case.name} — {result.error}")
        lines.append("")

    lines.append("## Tests executed")
    lines.append("")
    lines.append("| ID | Category | As tenant | Result | Severity |")
    lines.append("|---|---|---|---|---|")
    for result in run.results:
        status = {"pass": "pass", "fail": "**FAIL**", "error": "error"}[result.status]
        sev = result.severity.value if result.severity else "—"
        lines.append(
            f"| `{result.test_case.id}` | {result.test_case.category} | "
            f"{result.test_case.as_tenant} | {status} | {sev} |"
        )
    lines.append("")

    lines.extend(_limitations_block())
    return "\n".join(lines)


def _surface_summary(failures: list[TestResult]) -> list[str]:
    """Collapse findings by leaking surface.

    A single unscoped code path fails most of the suite at once. Twenty findings
    usually means one or two bugs, and a reader who cannot see that will either
    panic or stop reading. This table names the layers to fix.
    """
    buckets: dict[tuple[str, str], set[str]] = {}
    for result in failures:
        for match in result.matches:
            key = (match.location, match.marker.kind.value)
            buckets.setdefault(key, set()).add(result.test_case.id)

    lines = ["## Leak surfaces", "", "Where forbidden data became visible, and how many tests observed it.", ""]
    lines.append("| Surface | Marker kind | Severity | Tests affected |")
    lines.append("|---|---|---|---|")
    from .models import MarkerKind  # local import keeps the module import graph flat

    ordered = sorted(
        buckets.items(),
        key=lambda kv: (-MarkerKind(kv[0][1]).severity.rank, kv[0][0], kv[0][1]),
    )
    for (location, kind), test_ids in ordered:
        sev = MarkerKind(kind).severity.value
        lines.append(f"| {location} | {kind} | {sev} | {len(test_ids)} |")
    lines.append("")
    lines.append(
        f"_{len(failures)} failing tests across {len(buckets)} distinct surface(s). "
        "A single unscoped path typically fails many tests at once — fix the surface, not each test._"
    )
    lines.append("")
    return lines


def _finding_block(result: TestResult) -> list[str]:
    case = result.test_case
    sev = (result.severity or Severity.MEDIUM).value.upper()
    lines: list[str] = []
    lines.append(f"### {sev} — {case.name}")
    lines.append("")
    lines.append(f"**Test:** `{case.id}` ({case.category}) &nbsp;•&nbsp; **Sent as:** `{case.as_tenant}` (low-privilege user)")
    lines.append("")
    if case.rationale:
        lines.append(f"_{case.rationale.strip()}_")
        lines.append("")

    lines.append("**Observed leak**")
    lines.append("")
    lines.append("| Marker | Kind | Belongs to | Seen in |")
    lines.append("|---|---|---|---|")
    for match in result.matches:
        lines.append(
            f"| `{match.marker.value}` | {match.marker.kind.value} | "
            f"{match.marker.tenant_id} | {match.location} |"
        )
    lines.append("")

    for match in result.matches:
        if match.excerpt:
            lines.append(f"Context for `{match.marker.value}` in {match.location}:")
            lines.append("")
            lines.append("```")
            lines.append(match.excerpt)
            lines.append("```")
            lines.append("")

    lines.append("**Reproduce**")
    lines.append("")
    lines.append(f"1. Authenticate as the `{case.as_tenant}` low-privilege test user.")
    step = 2
    if case.prime:
        lines.append(f"2. First, as `{case.prime.as_tenant}`, send: `{case.prime.prompt}`")
        step = 3
    lines.append(f"{step}. Send this prompt:")
    lines.append("")
    lines.append("```")
    lines.append(case.prompt)
    lines.append("```")
    lines.append("")
    lines.append(f"{step + 1}. Observe the marker(s) above in the response.")
    lines.append("")

    lines.append("<details><summary>Full response</summary>")
    lines.append("")
    lines.append("```")
    lines.append(_truncate(result.observation.answer) or "(empty answer)")
    lines.append("```")
    if result.observation.citations:
        lines.append("")
        lines.append("Citations:")
        lines.append("")
        lines.append("```")
        for c in result.observation.citations:
            lines.append(f"- {c}")
        lines.append("```")
    if result.observation.metadata:
        lines.append("")
        lines.append("Metadata:")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(result.observation.metadata, indent=2, ensure_ascii=False))
        lines.append("```")
    lines.append("")
    lines.append("</details>")
    lines.append("")

    lines.append(f"**Recommended remediation.** {_remediation_for(result)}")
    lines.append("")
    lines.append("Re-run this test after the fix; it stays in the suite permanently as a regression check.")
    lines.append("")
    lines.append("---")
    lines.append("")
    return lines


def _limitations_block() -> list[str]:
    return [
        "## What this report does and does not show",
        "",
        "This report documents the tenant-isolation behaviour observed for the named "
        "application and build, under the tests listed above.",
        "",
        "**Tested:** whether a forbidden marker belonging to another tenant became visible "
        "through the AI interface — in answer text, citations, or metadata returned to the client.",
        "",
        "**Not tested:** internal retrieval authorization, reranker behaviour, tool-call "
        "arguments, observability traces, ACL synchronisation drift, and any path not exercised "
        "by the prompts above. A pass means these tests did not produce a leak. It is not proof "
        "that the application is secure, that no leak is possible, or that any regulatory "
        "obligation is satisfied.",
        "",
        "Findings are deterministic: each one is an exact string match on a marker planted in "
        "synthetic test data. No real customer data was used.",
        "",
    ]


def to_json(run: TestRun) -> str:
    return json.dumps(run.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


def write(run: TestRun, directory: str | Path, formats: list[str]) -> list[Path]:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if "markdown" in formats:
        p = out / f"{run.run_id}.report.md"
        p.write_text(to_markdown(run))
        written.append(p)
    if "json" in formats:
        p = out / f"{run.run_id}.report.json"
        p.write_text(to_json(run))
        written.append(p)
    return written
