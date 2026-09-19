"""Command line interface.

Exit codes are part of the contract. They gate pipelines, so they are narrower
than the verdict and do not always agree with it:

    0  nothing to act on in CI. Either a clean verified run (verdict `pass`),
       findings that all sat below --fail-on (verdict `fail`), or a locally
       waived debug run (verdict `incomplete`).
    1  the run proved nothing: a configured surface never resolved, ingest was
       never verified, or tests errored. Verdict `incomplete`.
    2  a leak at or above --fail-on. Verdict `fail`.

When the two differ, the verdict in the report is the one to cite. Exit 0 with
verdict `fail` is the dangerous case, so the terminal output shouts about it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from . import fixtures as fixtures_mod
from . import report as report_mod
from . import stamp
from .attacks import SuiteError, load_suite, resolve_suite_path, run_suite, validate_against_fixtures
from .config import DEFAULT_CONFIG, Config, ConfigError
from .connector import Connector, ConnectorError
from .detector import dropped_markers, scan
from .models import Severity, TestResult, TestRun

app = typer.Typer(
    add_completion=False,
    help="Pre-release negative test suite for cross-tenant data leakage in multi-tenant AI features.",
)
console = Console()

EXIT_OK = 0
EXIT_OPERATIONAL = 1
EXIT_LEAK = 2

CONFIG_PATH = "aitenant.yaml"


#: Environment variables set by the common CI providers. GitHub Actions,
#: GitLab, CircleCI, Travis and Buildkite all set CI=true; Jenkins and
#: TeamCity are detected by their own markers.
_CI_MARKERS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE", "TEAMCITY_VERSION")


def running_in_ci() -> str | None:
    """Return the marker naming the CI provider, or None when interactive."""
    for name in _CI_MARKERS:
        value = os.environ.get(name, "")
        if value and value.lower() not in {"0", "false", "no"}:
            return name
    return None


def _load_config(path: str) -> Config:
    try:
        return Config.load(path)
    except ConfigError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(EXIT_OPERATIONAL)


def _load_fixtures(config: Config):
    try:
        return fixtures_mod.load(config.fixtures_path)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(EXIT_OPERATIONAL)


@app.command()
def init(
    config_path: str = typer.Option(CONFIG_PATH, "--config", "-c"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config."),
) -> None:
    """Create a config file, generate synthetic fixtures, and export the corpus."""
    p = Path(config_path)
    if p.exists() and not force:
        console.print(f"[yellow]{p} already exists.[/yellow] Use --force to overwrite.")
    else:
        p.write_text(DEFAULT_CONFIG)
        console.print(f"[green]Wrote[/green] {p}")

    fs = fixtures_mod.default_fixture_set()
    saved = fixtures_mod.save(fs, "fixtures/fixtures.json")
    console.print(f"[green]Generated fixtures[/green] {saved}")
    written = fixtures_mod.write_markdown_corpus(fs, "fixtures/corpus")
    console.print(f"[green]Exported corpus[/green] {len(written)} documents under fixtures/corpus/")

    console.print()
    console.print("Next:")
    console.print("  1. Upload fixtures/corpus/tenant_a/ into your staging Tenant A")
    console.print("  2. Upload fixtures/corpus/tenant_b/ into your staging Tenant B")
    console.print("  3. Edit aitenant.yaml with your endpoint and auth")
    console.print("  4. [bold]aitenant verify-ingest[/bold]  (confirms the documents are actually retrievable)")
    console.print("  5. [bold]aitenant test[/bold]")


@app.command()
def configure(
    endpoint: str = typer.Option(None, "--endpoint", help="Target chat endpoint URL."),
    environment: str = typer.Option(None, "--environment"),
    build: str = typer.Option(None, "--build", help="Build or version identifier for the report."),
    config_path: str = typer.Option(CONFIG_PATH, "--config", "-c"),
) -> None:
    """Update endpoint, environment, or build id in the config file."""
    p = Path(config_path)
    if not p.exists():
        console.print(f"[red]No config at {p}.[/red] Run `aitenant init` first.")
        raise typer.Exit(EXIT_OPERATIONAL)

    raw = yaml.safe_load(p.read_text()) or {}
    if endpoint:
        raw.setdefault("endpoint", {})["url"] = endpoint
    if environment:
        raw["environment"] = environment
    if build:
        raw["build_id"] = build
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    console.print(f"[green]Updated[/green] {p}")


@app.command("gen-fixtures")
def gen_fixtures(
    output: str = typer.Option("fixtures/fixtures.json", "--output", "-o"),
    corpus: str = typer.Option("fixtures/corpus", "--corpus"),
) -> None:
    """Regenerate synthetic fixtures with fresh canaries."""
    fs = fixtures_mod.default_fixture_set()
    fixtures_mod.save(fs, output)
    written = fixtures_mod.write_markdown_corpus(fs, corpus)
    console.print(f"[green]Wrote[/green] {output} and {len(written)} documents under {corpus}/")
    console.print("[yellow]Canaries changed — re-upload the corpus before testing.[/yellow]")


@app.command("verify-ingest")
def verify_ingest(config_path: str = typer.Option(CONFIG_PATH, "--config", "-c")) -> None:
    """Confirm each tenant can retrieve its OWN fixtures.

    Run this before every engagement. If the documents never landed in the index,
    every leak test passes vacuously and the clean report is worthless.
    """
    config = _load_config(config_path)
    fs = _load_fixtures(config)

    table = Table("Tenant", "Document", "Own canary retrievable")
    all_ok = True
    # Union across every probe: a field may legitimately be absent from one
    # response and present in another.
    surfaces_seen = {"answer": False, "citations": False, "metadata": False}

    with Connector(config) as connector:
        for tenant in fs.tenants:
            for doc in tenant.documents:
                prompt = f"Summarise the document titled '{doc.title}' and quote its internal reference code."
                try:
                    obs = connector.ask(tenant.tenant_id, prompt)
                except ConnectorError as exc:
                    table.add_row(tenant.tenant_id, doc.title, f"[red]error: {exc}[/red]")
                    all_ok = False
                    continue
                for key, present in obs.schema_found.items():
                    surfaces_seen[key] = surfaces_seen.get(key, False) or present
                haystack = f"{obs.answer} {obs.citations} {obs.metadata}".casefold()
                found = any(c.casefold() in haystack for c in doc.canaries)
                table.add_row(tenant.tenant_id, doc.title, "[green]yes[/green]" if found else "[red]no[/red]")
                all_ok = all_ok and found

    console.print(table)
    console.print()

    ep = config.endpoint
    schema = Table("Surface", "Configured path", "Seen in responses", "Consequence if missing")
    configured = {
        "answer": ep.response_text_field,
        "citations": ep.citations_field,
        "metadata": ep.metadata_field,
    }
    consequences = {
        "answer": "nothing is tested at all",
        "citations": "cl-01, cl-02 pass vacuously",
        "metadata": "ml-01, ml-02 pass vacuously",
    }
    schema_ok = True
    for surface, path in configured.items():
        if path is None:
            schema.add_row(surface, "[dim]not configured[/dim]", "[dim]n/a[/dim]", "[dim]surface intentionally skipped[/dim]")
            continue
        seen = surfaces_seen.get(surface, False)
        schema.add_row(
            surface,
            f"`{path}`",
            "[green]yes[/green]" if seen else "[red]no[/red]",
            "" if seen else f"[yellow]{consequences[surface]}[/yellow]",
        )
        schema_ok = schema_ok and seen
    console.print(schema)

    if all_ok and schema_ok:
        written = stamp.write(config.fixtures_path, ep.url)
        console.print()
        console.print("[green]Fixtures are retrievable and every configured surface resolved.[/green] The suite will test real paths.")
        console.print(f"[dim]Stamped {written} — runs against these fixtures will record ingest as verified.[/dim]")
        raise typer.Exit(EXIT_OK)

    console.print()
    if not all_ok:
        console.print(
            "[red]Some fixtures are not retrievable by their own tenant.[/red] "
            "Until this passes, a clean leak report proves nothing — the documents may "
            "simply not be in the index. Check ingestion and reindexing."
        )
    if not schema_ok:
        console.print(
            "[red]A configured response path never resolved.[/red] "
            "Tests against that surface would scan an empty string and pass without "
            "testing anything. Fix the dotted path in your config, or set it to null "
            "to record that the surface is intentionally out of scope."
        )
    raise typer.Exit(EXIT_OPERATIONAL)


@app.command()
def test(
    suite: str = typer.Option("quick-leak-check", "--suite", "-s", help="Suite name or path."),
    config_path: str = typer.Option(CONFIG_PATH, "--config", "-c"),
    environment: str = typer.Option(None, "--environment"),
    build: str = typer.Option(None, "--build"),
    fail_on: Severity = typer.Option(Severity.MEDIUM, "--fail-on", help="Minimum severity that fails the build."),
    formats: str = typer.Option(None, "--format", help="Comma-separated: markdown,json"),
    allow_unverified_ingest: bool = typer.Option(
        False,
        "--allow-unverified-ingest",
        help=(
            "Manual debugging only. Runs without a passing verify-ingest. Refused in CI, "
            "and the result is still marked incomplete and unusable as evidence."
        ),
    ),
) -> None:
    """Run the leak suite against the configured endpoint."""
    # The waiver exists so someone can poke at a target from their laptop. In CI
    # it would silently convert the regression gate into theatre, so it is
    # refused rather than warned about.
    ci_marker = running_in_ci()
    if allow_unverified_ingest and ci_marker:
        console.print(
            f"[red]--allow-unverified-ingest is refused in CI[/red] (detected via ${ci_marker})."
        )
        console.print(
            "It is a manual debugging aid, not a way to make a pipeline green. Without a "
            "passing verify-ingest the canaries may not exist in the index, so the suite would "
            "gate on nothing. Run [bold]aitenant verify-ingest[/bold] as a prior step instead."
        )
        raise typer.Exit(EXIT_OPERATIONAL)

    config = _load_config(config_path)
    if environment:
        config.environment = environment
    if build:
        config.build_id = build
    fs = _load_fixtures(config)

    try:
        suite_path = resolve_suite_path(suite)
        suite_name, cases = load_suite(suite_path)
        validate_against_fixtures(cases, fs)
    except SuiteError as exc:
        console.print(f"[red]Suite error:[/red] {exc}")
        raise typer.Exit(EXIT_OPERATIONAL)

    skipped = dropped_markers(fs.forbidden_for(cases[0].as_tenant))
    if skipped:
        console.print(
            f"[yellow]{len(skipped)} marker(s) excluded as non-discriminating[/yellow] "
            f"(too short or too generic to assert on): {sorted({m.value for m in skipped})}"
        )

    console.print(f"Running [bold]{suite_name}[/bold] ({len(cases)} tests) against {config.endpoint.url}")
    console.print()

    def progress(result: TestResult) -> None:
        mark = {"pass": "[green]pass[/green]", "fail": "[red]FAIL[/red]", "error": "[yellow]err [/yellow]"}[result.status]
        sev = f" ({result.severity.value})" if result.severity else ""
        console.print(f"  {mark} {result.test_case.id:<6} {result.test_case.name}{sev}")

    run = run_suite(
        config,
        fs,
        cases,
        suite_name,
        on_result=progress,
        require_ingest_verification=not allow_unverified_ingest,
    )

    fmts = [f.strip() for f in formats.split(",")] if formats else config.report.formats
    written = report_mod.write(run, config.report.directory, fmts)
    console.print()
    for path in written:
        console.print(f"[green]Report:[/green] {path}")

    _, _, ingest_reason = stamp.read(config.fixtures_path, config.endpoint.url)
    _summarise_and_exit(run, fail_on, ingest_reason)


@app.command()
def report(
    run_json: str = typer.Argument(..., help="Path to a saved .report.json"),
    output: str = typer.Option(None, "--output", "-o", help="Write Markdown here instead of stdout."),
) -> None:
    """Re-render a saved JSON run as Markdown."""
    p = Path(run_json)
    if not p.exists():
        console.print(f"[red]No such file:[/red] {p}")
        raise typer.Exit(EXIT_OPERATIONAL)
    run = TestRun.model_validate(json.loads(p.read_text()))
    markdown = report_mod.to_markdown(run)
    if output:
        Path(output).write_text(markdown)
        console.print(f"[green]Wrote[/green] {output}")
    else:
        typer.echo(markdown)


def _summarise_and_exit(run: TestRun, fail_on: Severity, ingest_reason: str = "") -> None:
    failures = run.failures
    errors = run.errors
    summary = run.summary

    if errors:
        console.print(f"[yellow]{len(errors)} test(s) could not run — those boundaries were not tested.[/yellow]")

    # Report every completeness problem, whatever the verdict turns out to be.
    if summary and not summary.contract_matched:
        console.print()
        console.print("[red]Connector contract not matched.[/red]")
        for item in summary.scope_not_tested:
            if "never resolved" in item:
                console.print(f"  [yellow]•[/yellow] {item}")
        console.print(
            "Those surfaces were scanned as an empty string, so their tests passed without "
            "testing anything. Fix the dotted paths in your config, or set them to null to "
            "record them as out of scope."
        )

    if summary and not summary.ingest_verified:
        console.print()
        console.print("[red]Ingest verification has not passed for these fixtures.[/red]")
        if ingest_reason:
            console.print(f"  [yellow]•[/yellow] {ingest_reason}")
        console.print(
            "Canary presence in the index was never confirmed, so a clean result proves "
            "nothing. Run [bold]aitenant verify-ingest[/bold] first."
        )
        if summary.ingest_verification_waived:
            console.print(
                "[yellow]Waived by --allow-unverified-ingest — debugging only.[/yellow] "
                "This run is not evidence and must not be attached to a security review. "
                "The waiver is recorded in the report."
            )
        else:
            console.print(
                "[dim]For manual debugging only, --allow-unverified-ingest runs anyway.[/dim]"
            )

    console.print()

    # Always state the verdict verbatim, so the terminal and the report can
    # never be read as disagreeing.
    if summary:
        colour = {"pass": "green", "fail": "red", "incomplete": "yellow"}[summary.verdict]
        console.print(f"Verdict: [{colour}][bold]{summary.verdict.upper()}[/bold][/{colour}]")

    # Precedence: a leak outranks everything. Every completeness problem above
    # causes false negatives, never false positives — so a finding is real
    # evidence even when the run around it was unsound.
    if failures:
        gating = [r for r in failures if r.severity and r.severity.rank >= fail_on.rank]
        worst = run.worst_severity
        console.print(
            f"[red]FAIL[/red] — {len(failures)} of {len(run.results)} tests leaked data "
            f"(highest severity: {worst.value if worst else 'unknown'})."
        )
        if summary and not summary.run_complete:
            console.print(
                "[yellow]The run was also incomplete — these findings are real, but other "
                "boundaries went untested.[/yellow]"
            )
        if not gating:
            # The most dangerous state in the tool: real leaks, green shell.
            # Anyone skimming CI output must not read this as "nothing found".
            by_sev = ", ".join(
                f"{count} {sev}"
                for sev, count in (
                    ("critical", summary.critical_findings if summary else 0),
                    ("high", summary.high_findings if summary else 0),
                    ("medium", summary.medium_findings if summary else 0),
                )
                if count
            )
            console.print()
            console.print(
                f"[bold yellow]!! LEAKS FOUND BELOW THRESHOLD — EXITING 0 !![/bold yellow]"
            )
            console.print(
                f"[yellow]{by_sev or 'findings'} recorded. These are real leaks.[/yellow] "
                f"The build is green only because --fail-on was set to "
                f"[bold]{fail_on.value}[/bold]. Lower the threshold to gate on them."
            )
            raise typer.Exit(EXIT_OK)
        raise typer.Exit(EXIT_LEAK)

    if summary and not summary.run_complete:
        console.print(
            "[red]INCOMPLETE[/red] — no leaks were observed, but the run did not prove the "
            "boundary held. This result is not usable as evidence."
        )
        # An explicit waiver is the operator accepting that, so do not also fail
        # their shell. The verdict in the report stays incomplete regardless.
        only_ingest_missing = summary.contract_matched and not errors
        if summary.ingest_verification_waived and only_ingest_missing:
            raise typer.Exit(EXIT_OK)
        raise typer.Exit(EXIT_OPERATIONAL)

    console.print(
        f"[green]PASS[/green] — {len(run.results)} tests, no cross-tenant leakage observed "
        "on the surfaces listed under Scope exercised."
    )
    raise typer.Exit(EXIT_OK)


if __name__ == "__main__":
    app()
