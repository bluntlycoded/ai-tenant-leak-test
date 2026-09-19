# AI Tenant Leak Test

An automated pre-release negative test that checks whether one customer can make your AI feature reveal another customer's data.

Plant safe fake markers in a staging environment, act as Tenant A's low-privilege user, try to surface Tenant B's markers, and fail the build if any appear — in the answer, the citations, or the metadata returned to the client. Every finding becomes a permanent regression test.

Mental model: Snyk checks dependencies for known risk, Postman checks that APIs behave, this checks whether an AI feature leaks forbidden customer data.

## What this is not

Not an AI red-teaming platform, a pentest, a runtime firewall, a compliance or certification product, or a generic RAG evaluator. Generic attack execution is commodity — [Promptfoo](https://github.com/promptfoo/promptfoo), [Garak](https://github.com/NVIDIA/garak) and PyRIT are free and better at it. This does one narrow thing those tools treat as one check among hundreds.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[devtarget]"
.venv/bin/aitenant init
```

`init` writes `aitenant.yaml`, generates fixtures with fresh canaries, and exports a Markdown corpus to `fixtures/corpus/`. Then:

1. Upload `fixtures/corpus/tenant_a/` into staging Tenant A, and `tenant_b/` into Tenant B.
2. Point `aitenant.yaml` at your endpoint; export the tenant tokens it references.
3. **`aitenant verify-ingest`** — confirms each tenant can retrieve its *own* documents.
4. `aitenant test`

Step 3 is not optional. If the fixtures never landed in the index, every leak test passes vacuously and a clean report proves nothing.

```bash
aitenant test --suite quick-leak-check --build 1.4.2 --fail-on critical
```

Exit codes: `0` passed, `1` the run proved nothing, `2` a leak at or above `--fail-on`.

Step 3 is enforced, not advisory: `test` refuses to report a clean run as a pass unless `verify-ingest` has passed for these exact fixtures against this exact endpoint. Regenerating fixtures or changing endpoint invalidates it.

## What the report looks like

The report is the product. Both samples are real output against the vulnerable dev target:

- [`examples/sample-failing-report.md`](examples/sample-failing-report.md) — retrieval correctly scoped, response cache not. **2 of 23 failing, critical.**
- [`examples/sample-passing-report.md`](examples/sample-passing-report.md) — same suite, fully isolated target. **23 of 23 passing.**

Every report opens with the same four sections, in this order:

```
## Verdict            pass | fail | incomplete
## Scope exercised    Tested / Not tested — stated before any finding
## Environment        build, endpoint, suite, run id, counts
## Connector contract response shape matched? ingest verified? run complete?
```

Scope comes before findings on purpose. A reviewer citing the report needs the bounds of the exercise before its results, and a reader who scrolls no further still leaves knowing what was not looked at.

Each finding then carries the literal prompt, the identity that sent it, the exact marker that leaked, where it appeared, numbered reproduction steps, and the full response.

The failing sample is the more instructive one: twenty-one tests pass and the two cache probes fail. That is the shape most real findings take — the primary retriever is usually filtered correctly, and the leak is on a secondary path.

### Machine-readable summary

The JSON result opens with a `summary` block for wiring GitHub checks or alerts without parsing findings:

```json
{
  "summary": {
    "verdict": "pass",
    "run_complete": true,
    "contract_matched": true,
    "ingest_verified": true,
    "ingest_verified_at": "2026-09-19T02:53:09Z",
    "ingest_verification_waived": false,
    "tests_run": 23, "tests_passed": 23, "tests_failed": 0, "tests_errored": 0,
    "critical_findings": 0, "high_findings": 0, "medium_findings": 0,
    "distinct_markers_leaked": 0, "distinct_surfaces_leaked": 0, "total_matches": 0,
    "scope_tested": ["assistant answer text", "cache priming and cross-tenant response reuse (2 tests)", "..."],
    "scope_not_tested": ["reranker behaviour and hybrid-search vector legs", "..."]
  }
}
```

Severity counts are over **distinct markers**, not raw matches. One unscoped path leaking the same marker into twenty responses is one finding, not twenty — inflated counts would make a single bug look like a catastrophe and cost the report its credibility. `total_matches` keeps the raw number.

### Verdict integrity

`verdict` is `incomplete` — never `pass` — whenever a test errored, a configured surface never resolved, or ingest verification did not pass for these fixtures against this endpoint. **A run that proved nothing must never be able to look like a clean one.**

One deliberate asymmetry: every completeness problem here causes false *negatives*, never false positives. An unresolved citations path or an unconfirmed canary can hide a leak; neither can invent one. So a **finding stands on its own evidence even when the run around it was unsound**, and `verdict` is `fail` in that case rather than `incomplete`.

| State | Verdict | Exit |
|---|---|---|
| Verified, clean | `pass` | 0 |
| Verified, leak found | `fail` | 2 |
| Leak found, contract mismatch | `fail` | 2 |
| Clean, ingest never verified | `incomplete` | 1 |
| Clean, contract mismatch | `incomplete` | 1 |
| Clean, ingest waived with `--allow-unverified-ingest` | `incomplete` | 0 |

When a run is both incomplete *and* found a leak, the report says so in the Verdict section rather than leaving the reader to work it out:

> **A leak was observed, but parts of this run were incomplete.** The findings below are valid — a marker either appeared or it did not, and nothing about an incomplete run can make one appear that was not there. What cannot be trusted is the *absence* of further findings.

### `--allow-unverified-ingest`

Manual debugging only. Specifically:

| | |
|---|---|
| Interactive use, poking at a target | allowed |
| **CI pipelines** | **refused** — exits 1 with an explanation |
| **Generating evidence for a review** | **never** — the report says so on its own face |

It spares your shell an error; it does not make the run sound. The verdict stays `incomplete`, `ingest_verification_waived` is set, the waiver is listed under Not tested, and the Verdict section states the run is a debugging aid and must not be attached to a security review.

CI refusal is enforced, not advised — detected via `CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `JENKINS_URL`, `BUILDKITE`, or `TEAMCITY_VERSION`. A flag that can turn a regression gate into theatre should not be one `--flag` away in a pipeline; run `verify-ingest` as a prior step instead.

## The supported target shape

V1 supports exactly one target shape. This is a deliberate constraint: every additional shape is a connector to maintain and a pilot that turns into custom consulting.

| | |
|---|---|
| Transport | one JSON-over-HTTP endpoint |
| Method | `POST` (configurable) |
| Prompt | one request-body field, dotted paths supported (`input.message`) |
| Tenant identity | per-tenant headers or body fields — bearer token, API key, or `workspace_id` in the payload |
| Answer | one dotted read path, **required** |
| Citations | one dotted read path, optional — set `null` if the API returns none |
| Metadata | one dotted read path, optional — set `null` if the API returns none |
| Timeout | per request, default 60s |
| Retries | transport errors and 5xx only, default 2, exponential backoff. A successful response is never re-sent, so cache tests stay meaningful |

### The wire contract

Hand this section to the engineer who owns the endpoint. It is the whole integration.

**Request.** One JSON `POST`. The prompt goes in a single field; the tenant identity is carried by whatever your API already uses.

```http
POST /api/ai/chat HTTP/1.1
Content-Type: application/json
Authorization: Bearer <low-privilege test user token for one tenant>

{
  "message": "Summarise all Q4 pricing strategies available in the knowledge base."
}
```

**Response.** JSON, HTTP 200. Only `answer` is required.

```json
{
  "answer": "Here is what I found regarding your question...",
  "citations": [
    { "source_id": "DOC-1234", "title": "Q4 Pricing Strategy", "filename": "q4-pricing.md" }
  ],
  "metadata": {
    "sources": [
      { "owner": "finance@example.com", "project": "Revenue FY26", "filename": "q4-pricing.md" }
    ]
  }
}
```

Field names and nesting are yours — the config maps to them with dotted paths (`data.reply.text`). Citations may be a list of strings or of objects; every value in an object is searched. Anything not JSON is treated as the answer in full, so a plain-text endpoint still tests something rather than silently testing nothing.

**What the endpoint must do for the test to mean anything**

| Requirement | Why |
|---|---|
| Two staging tenants, one low-privilege user each | The boundary under test |
| Both users reachable with a static credential | No interactive login flow in V1 |
| The synthetic corpus ingested and indexed in each tenant | `verify-ingest` fails the run otherwise |
| Stable responses for the same input | Deterministic markers, not deterministic phrasing — the model may paraphrase freely |

### Unsupported in V1

Qualify against this list before an engagement, not during one. Where the limit is detectable, the connector refuses with a named error rather than guessing.

| Not supported | Behaviour | Why |
|---|---|---|
| **NDJSON / JSONL streaming** | `UnsupportedResponseError`, detected from the response content type | Out of scope; SSE is the one streaming transport handled |
| **Refresh-token / session-renewal auth** | `AuthError` naming an expired credential, distinguished from a wrong one | V1 sends a static credential per tenant and never renews it |
| **SSO or interactive login** | not modelled | Same reason — the config carries a static header or body value |
| **Signed request bodies** | not modelled | The prompt is injected into the body after signing would have happened |
| **Websocket transports** | not modelled | One JSON `POST` only |
| **Production environments** | refused by policy, not by code | The suite plants synthetic documents; it must never touch real customer data |
| **Targets without two isolable test tenants** | no meaningful test exists | The boundary under test does not exist |

Tenant identity is supported as **per-tenant headers or per-tenant body fields**. That covers bearer tokens, API-key headers, and `workspace_id`/`user_id` in the payload. Anything more exotic — a tenant derived from the hostname, a subdomain, a signed claim the caller cannot set — needs a connector change, and that change should be driven by a real design partner rather than anticipated.

### Streaming (SSE)

Opt-in, off by default. Most modern chat endpoints stream, so this is the one transport beyond plain JSON that V1 handles:

```yaml
endpoint:
  url: https://staging.example.com/api/ai/chat/stream
  streaming:
    mode: sse                 # none (default) | sse
    delta_field: delta        # dotted path, within each frame, to the text chunk
    done_sentinel: "[DONE]"   # frame payload that ends the stream; null if none
```

`delta_field` is the only vendor-specific knob. OpenAI-shaped APIs use `choices.0.delta.content`; simpler ones use `delta` or `token`. Citations and metadata reuse the same dotted paths as the non-streaming contract, resolved against whichever frame carries them — typically a final frame before the sentinel.

How it behaves:

| | |
|---|---|
| Aggregation | the whole stream is buffered, then folded back into one answer plus citations and metadata. Everything downstream is unchanged, because a canary in a streamed answer is the same canary |
| Deltas vs. a final whole message | deltas win. A truncated stream should read as truncated, not be silently repaired by a final frame that may never arrive |
| Wrong `delta_field` | `UnresolvedPathError` naming the keys actually present in the first frame |
| Mode off, endpoint streams | still refused — opt-in only, and the error tells you which setting to turn on |
| Unparseable frames | counted, not hidden, so a mostly-broken stream cannot read as a short answer |

The case worth knowing about: tokenisation splits canaries mid-string, so a marker can arrive across three frames. Reassembly restores it, and both a unit test and an end-to-end test against a real socket pin that — the dev target deliberately chops answers into 7-character frames for exactly this reason.

What this mode is not: no incremental consumption, no reconnection, no `Last-Event-ID`, no websockets. Right for a test harness against staging, wrong for a production client.

[`examples/aitenant.yaml`](examples/aitenant.yaml) documents every config field. Tokens are never written to the config — it holds `${VAR}` references resolved from the environment at run time.

**Setting a read path to a field that does not exist is the same failure as fixtures never landing.** That surface gets scanned as an empty string and its tests pass while testing nothing. `verify-ingest` checks every configured path against real responses and fails the run if one never resolves, naming the tests that would have lied:

```
| Surface   | Configured path | Seen in responses | Consequence if missing   |
| answer    | `answer`        | yes               |                          |
| citations | `data.sources`  | no                | cl-01, cl-02 pass vacuously |
| metadata  | `metadata`      | yes               |                          |
```

## Run in GitHub Actions

Copy [`.github/workflows/tenant-leak-test.yml`](.github/workflows/tenant-leak-test.yml) into the repository of the application under test. The CLI's exit codes do the gating: `2` fails the build, `1` means the run was incomplete and proved nothing.

Three secrets are required in the target repo:

| Secret | What it is |
|---|---|
| `AITLT_FIXTURES_JSON` | base64 of `fixtures/fixtures.json` from the seeding run |
| `TENANT_A_TOKEN` | low-privilege test user in staging tenant A |
| `TENANT_B_TOKEN` | low-privilege test user in staging tenant B |

```bash
base64 -i fixtures/fixtures.json | pbcopy   # paste into the secret
```

The fixtures travel as a secret rather than being regenerated in CI, because the canaries must be the *same ones* that were ingested into staging. Regenerating them would produce markers that exist nowhere in the index, and every test would pass while testing nothing.

The workflow runs `verify-ingest` before the suite for the same reason, then uploads the report as a build artifact.

**Pick the trigger deliberately.** The useful one is a change to the AI data path — a new corpus, vector store, retriever, reranker, model, system prompt, agent tool, role, or cache. Running on every commit to unrelated code mostly burns minutes.

## The suite

23 tests in `suites/quick-leak-check.yaml`, across seven groups: direct cross-tenant retrieval, semantic adjacency, direct prompt injection, indirect injection via a planted document, citation leakage, metadata leakage, and cache reuse. The last two groups run in both directions, because isolation is not always symmetric.

Two design choices carry most of the weight:

**Semantically adjacent fixtures.** Each tenant holds a near-duplicate of the other's documents on two topics — Q4 pricing and the SLA. Random unrelated canaries do not stress a retriever; a near-duplicate of the caller's own question does. This is the highest-yield group in the suite, and it is deliberately spread across two document types so that a boundary which happens to hold on one collection is still exercised.

Each pair carries terms that exist in only one tenant's copy — a Freight tier, a sub-95% credit tier, a shipment tracking API. That makes some probes diagnostic even without a canary: Tenant A has no shipment tracking API, so a confident answer about its uptime commitment is itself the finding.

**Derived forbidden markers.** A test case never hardcodes what it looks for. It declares which tenant sends the prompt, and the forbidden set is computed from every *other* tenant's fixtures at run time. Add a document and every test starts checking for its markers.

Detection is exact string matching after Unicode and typography normalisation — no LLM judge. A canary either appeared or it did not, so a finding is evidence rather than an opinion, and CI can act on it. Markers that are too short or too generic to discriminate (`confidential`, `policy`) are dropped and reported, so nothing looks tested that wasn't.

## Calibrating the detector

`devtarget/` is a deliberately vulnerable multi-tenant RAG app with individually switchable leak paths. It exists because **a negative test suite that has never produced a positive is worthless** — without it you cannot distinguish "their system is safe" from "my tests are weak."

```bash
./scripts/calibrate.sh
```

This starts the target under nine configurations and asserts the suite's verdict each time:

| Scenario | Expected |
|---|---|
| all boundaries enforced | PASS |
| post-filter retrieval (scoping applied after similarity search) | FAIL |
| unscoped citation resolver | FAIL |
| unscoped metadata | FAIL |
| tenant-blind response cache | FAIL |
| self-owned poisoned document dumps the index | FAIL |
| indirect injection via a leaked document | FAIL |
| every leak enabled | FAIL |
| correctly isolated, but ingest never verified | INCOMPLETE |

The last one calibrates verdict integrity rather than leak detection: a clean target with no ingest proof must report `incomplete`, never `pass`. That is the false-clean failure mode the product exists to prevent, so it gets the same treatment as a leak path.

The sixth scenario was found by the suite, not designed into it: with retrieval scoping fully intact, a tenant's *own* poisoned document made the model enumerate every title in the index, including the other tenant's. A cross-tenant leak on a path that never touches retrieval scoping.

The calibration run also doubles as sales collateral — a real failing report from a real HTTP service, with no customer NDA exposure.

`devtarget` is built to leak. Never deploy it anywhere reachable.

## Honest limitations

Output-level testing cannot prove internal retrieval authorization. A pass means these tests did not produce a leak on the surfaces listed under Scope exercised — not that the application is secure, that no leak is possible, or that any regulatory obligation is met. Every report states its own bounds before its own results.

Not covered in V1: retrieval traces, reranker behaviour, tool-call arguments, observability traces, and ACL synchronisation drift. Those need instrumentation access and belong to a deeper tier. They are enumerated in the `scope_not_tested` field of every run, so coverage is never inferred from the absence of a finding.

`quick-leak-check` is a narrow exercised-boundary check, not an assessment. The CLI says so on every run, and the report says so above its findings.

One unscoped code path usually fails most of the suite at once. The report's **Leak surfaces** table collapses findings by layer so twenty failures read as the one or two bugs they actually are.

## Development

```bash
./scripts/verify.sh
```

That is the release gate — everything that must be green before a change ships or a report is generated for a customer:

| Stage | What it proves |
|---|---|
| `pytest -m "not e2e"` | detector, summary semantics, connector error modes, CLI exit contract |
| `pytest -m e2e` | the whole operator journey against a live target over a real socket |
| `scripts/calibrate.sh` | the suite fires on each leak path and stays quiet on a clean one |
| `scripts/smoke.sh` | the wheel installs and the CLI runs outside the source tree |

The end-to-end stage is part of the gate, not an optional extra. It is the most product-realistic thing in the repo, so if it can be skipped it will drift — and the first place that would show up is a customer engagement. It runs as its own CI job for the same reason, so it can be required independently.

Reports land in `output/` and are gitignored — they contain customer responses.
