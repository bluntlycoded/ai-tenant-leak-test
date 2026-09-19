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

Exit codes: `0` passed, `1` operational error (nothing was proven), `2` a leak at or above `--fail-on`.

## The suite

20 tests in `suites/quick-leak-check.yaml`, across seven groups: direct cross-tenant retrieval, semantic adjacency, direct prompt injection, indirect injection via a planted document, citation leakage, metadata leakage, and cache reuse. The last two groups run in both directions, because isolation is not always symmetric.

Two design choices carry most of the weight:

**Semantically adjacent fixtures.** Tenant B holds a near-duplicate of Tenant A's pricing document. Random unrelated canaries do not stress a retriever; a near-duplicate of the caller's own question does. This is the highest-yield group in the suite.

**Derived forbidden markers.** A test case never hardcodes what it looks for. It declares which tenant sends the prompt, and the forbidden set is computed from every *other* tenant's fixtures at run time. Add a document and every test starts checking for its markers.

Detection is exact string matching after Unicode and typography normalisation — no LLM judge. A canary either appeared or it did not, so a finding is evidence rather than an opinion, and CI can act on it. Markers that are too short or too generic to discriminate (`confidential`, `policy`) are dropped and reported, so nothing looks tested that wasn't.

## Calibrating the detector

`devtarget/` is a deliberately vulnerable multi-tenant RAG app with individually switchable leak paths. It exists because **a negative test suite that has never produced a positive is worthless** — without it you cannot distinguish "their system is safe" from "my tests are weak."

```bash
./scripts/calibrate.sh
```

This starts the target under eight configurations and asserts the suite's verdict each time:

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

The sixth scenario was found by the suite, not designed into it: with retrieval scoping fully intact, a tenant's *own* poisoned document made the model enumerate every title in the index, including the other tenant's. A cross-tenant leak on a path that never touches retrieval scoping.

The calibration run also doubles as sales collateral — a real failing report from a real HTTP service, with no customer NDA exposure.

`devtarget` is built to leak. Never deploy it anywhere reachable.

## Honest limitations

Output-level testing cannot prove internal retrieval authorization. A pass means these tests did not produce a leak under the conditions tested — not that the application is secure, that no leak is possible, or that any regulatory obligation is met. Every report says so in its own words.

Not covered in V1: retrieval traces, reranker behaviour, tool-call arguments, observability traces, and ACL synchronisation drift. Those need instrumentation access and belong to a deeper tier.

One unscoped code path usually fails most of the suite at once. The report's **Leak surfaces** table collapses findings by layer so twenty failures read as the one or two bugs they actually are.

## Development

```bash
.venv/bin/python -m pytest tests/ -q   # detector unit tests
./scripts/calibrate.sh                 # end-to-end calibration
```

Reports land in `output/` and are gitignored — they contain customer responses.
