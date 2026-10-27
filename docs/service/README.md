# Sleuth incident service — planning docs

Planning output for the paid incident-investigation and monitoring add-on to Sleuth (2026-10-26). Everything here is
additive to the read-only CLI; nothing in these docs changes a CLI contract. Status tags on factual claims:
**VERIFIED** (checked against the repo, the live RPC, or a primary document this session), **INFERRED** (reasoning),
**UNSURE** (not checked — verify before relying on it). Build-status tags: **IMPLEMENTED** (code on this branch, offline test),
**PARTIAL** (built with named gaps), **DEFERRED** (designed, intentionally not built), **NOT BUILT** (mentioned in a design round
only). The "Implemented status" table at the top of `02-architecture.md` is authoritative; `11-running.md` says how to run it.
These docs were written while the code was being built and were corrected afterwards against the repo (Codex docs review,
30 findings, 2026-10-26); where a design section and a status table disagree, the status table wins.

## Summary

Sleuth already has the hard part: a deterministic, three-valued analysis kernel for Robinhood Chain token flows,
fail-closed log retrieval, and a chain-specific launch collector. What it lacked was a service boundary (collectors that
return partial results instead of mutating global state), durable case/evidence storage, a real monitoring loop
(a saved watchlist is not monitoring), payments kept apart from evidence collection, and a private case workspace.
Three Codex/Claude design rounds plus a skeptic pass converged on a NARROW build, and this branch implements its core:
free screens that show findings before any paywall, a $29 self-service pilot case with 30-day monitoring (manual
USDC order, operator-confirmed), a JSON+HTML evidence export, and a flag-gated x402-style machine screen with a mock
facilitator. The human-reviewed reporting tier is postponed pending counsel and a real-victim validation. Known gaps
that remain after the build: the rug jobs still call `triage`/`devhistory`/`trace`, which write the CLI watchlist and
discard partial work on budget expiry; there is no email provider, no real facilitator, no cookie exchange for the case
token, and no schema migration tool.

## Recommendation (NARROW)

| Build now | Postpone | Reject |
|---|---|---|
| Free token-rug screen with full headline findings and a coverage statement | Human-reviewed reporting tier ($249) — needs counsel on CA/NV private-investigator definitions and evidence that exchanges act on packages | Any project token |
| Free wallet-drain **event** screen (beta; event-named predicates, never a clean negative) | Chainabuse partner API / exchange / IC3 submission code paths | Success fees or any recovery claim |
| $29 self-service pilot case: deeper deterministic passes, private workspace, export, 30 days monitoring, no human investigation | $9/month monitoring extension | Selling victim data, paid removal of findings, per-accusation incentives |
| x402-style `exact` gate on the machine screen, flag-gated, mock facilitator only (pilot's own wire shape, replayable but not stateless — see 04) | Postgres (only on a measured trigger) | Charging for access to government channels |

Recommended validation gate (skeptic's test): offer free screens to 20 recent real victims; proceed with the paid tier
only if about 5 of 20 will pre-pay and at least one exchange/Chainabuse action follows a package. Whether this gate is
binding is Bo's decision B-2: the code enables checkout the moment `SLEUTH_PAY_TO_BASE_USDC` is set, so the variable
stays unset until B-2 is decided.

## Index

| Doc | Contents |
|---|---|
| [00-recommendation.md](00-recommendation.md) | Product recommendation, for/against, customers, comparison with free/paid services |
| [01-user-journeys.md](01-user-journeys.md) | Rugged / drained / pre-trade journeys, what is never asked |
| [02-architecture.md](02-architecture.md) | One-host FastAPI + worker + SQLite WAL, collector contract, data model, monitoring, manifests |
| [03-autonomy-and-reporting.md](03-autonomy-and-reporting.md) | Autonomy vs human review, three-valued predicates, reporting channels and permissions |
| [04-pricing-and-payments.md](04-pricing-and-payments.md) | Tiers, unit economics, success-fee assessment, checkout adapter, x402 design |
| [05-security-and-abuse.md](05-security-and-abuse.md) | Threat model and controls |
| [06-tests-and-ops.md](06-tests-and-ops.md) | Test plan, acceptance criteria, deployment, outages, backups, cost |
| [07-roadmap-and-backlog.md](07-roadmap-and-backlog.md) | MVP vs later, sequenced backlog with repo paths, external dependencies |
| [08-decisions-for-bo.md](08-decisions-for-bo.md) | Decisions only Bo can make |
| [09-design-record.md](09-design-record.md) | Codex/Claude disagreement and resolution record |
| [10-research-notes.md](10-research-notes.md) | RPC measurements, launchpad calldata, market, x402, reporting channels, licensing, EIP-7702 |
| [11-running.md](11-running.md) | Running the implemented pilot locally: env vars, tests, admin, limits |
| [design-rounds/](design-rounds/) | The seven round files, verbatim |

## What this phase does not authorize

Production deployment, spending on external services, sending reports or messages to anyone, public accusations,
moving funds, or indefinite monitoring without an agreed scope. No facilitator, payment-provider, email or paid-RPC
credentials are configured.
