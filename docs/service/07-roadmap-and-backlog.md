# 07 — Roadmap and sequenced backlog

## Done / Next / Later

| DONE (branch `feat/incident-service`, 2026-10-26) | NEXT (before any customer) | LATER |
|---|---|---|
| Collectors package returning partial results (`sleuth/collectors/`); drain investigation (`sleuth/collectors/drain.py`, 2026-10-27): multi-chain, current 7702 delegation per chain shown in red with its origin ("set in loss transaction X by Y") and "revoking approvals does not undo it", poisoning quarantine, self-authorised third-party moves set aside, per-tx mechanism, suspect profiles with exchange / bridge / privacy-pool outbound; `/tx` explainer | Replayability refactor of `triage`/`devhistory`/`trace` as libraries (no watchlist write, partial receipt returns) | Full native / internal history (drain), historical EIP-7702 index, paid drain case, historical delegation timeline (when did the type-4 land) |
| Free token-rug screen, free drain event screen + drain investigation follow-up (beta), transaction explainer (beta) | Receipt collector with hash-list checkpoints; write `coverage_checkpoints` | $9/month extension; human-reviewed tier (after B-3) |
| $29 pilot case: deep pass, workspace, export, 30-day Transfer monitoring | Capability-token cookie exchange; emailed link re-issue; email provider for alerts | Postgres on trigger; retention job (B-8) |
| SQLite WAL core, lease queue, two-cursor monitoring with reorg rollback, outbox with file sender | Structured coverage fields on every result (start, finality, provider, missing spans) and in the manifest | Chainabuse partner API, exchange drafting, authorization workflow |
| Manual USDC order with operator attestation; x402-style gate with mock facilitator behind a flag | Real facilitator + protocol-compatible wire shape (B-4); RPC check of attested Base tx; checkout provider adapter | Launchpad / B2B partnerships, Permit2, NFTs, cross-chain evidence |
| Deterministic JSON + HTML export with recomputable manifest | Schema migration tool (today: delete scratch db) | Public search page (B-9) |
| Public pages: home, methodology, corrections (no search) | Import-boundary test; cross-case isolation test; launchd units + backup script | — |

## Backlog with repo paths

- **Triage venue rule needs quote-flow evidence** — `sleuth/flows.py::detect_pools` marks any contract with net token flow both ways as a pool; RAID's game contract (receives from the launcher, pays players) was therefore a "sell" venue and the screen said "launcher sold" where the money trail shows nothing was paid. Fix: a venue must have paid native/ERC-20 value to sellers (internal tx or token credit) in at least one sell tx; otherwise classify as sink. Changes a CLI verdict, so version `triage` and note it in the CLI docs.
- **Money trail exit list is config-only** — `money-trail/0.2` counts a hop as an "exit" only when the recipient is in `known_services` (3 entries on chain 4663). Busy unlabeled EOAs are reported as `high volume`, never as exchanges, so `proceeds_reached_bridge_or_exchange` stays unknown for most real trails until a reviewed allowlist of bridge solvers / exchange hot wallets on Robinhood Chain exists (`config.yaml` → `known_services`).
- **Money trail follow-ups** — follow ERC-20 quote assets into DEX positions and bridges past hop 1; add the dev fee recipient and the launch funding source (Relay solver → launcher) as trail roots; per-case cache of explorer pages so the paid deep pass reuses them.

The collector boundary, service core, payments and website items are DONE as listed in the status table in 02; the
remaining sequence:

1. **Replayability refactors (behind the CLI)** — `sleuth/triage.py`, `sleuth/devhistory.py`: library mode that skips
   `Watchlist.save()` (flag, default unchanged for the CLI); receipt-loop partial returns in `sleuth/client.py` /
   `sleuth/devhistory.py`; `sleuth/trace.py` partial dict on timeout. Must not change CLI output for existing fixtures
   (STOXPREPS regression). Until this lands, 09 entry 2 stays UNRESOLVED and `SLEUTH_X402` stays off.
2. **Receipt collector** — `sleuth/collectors/receipts.py` (hash-list checkpoints: completed / missing / unattempted),
   wire `coverage_checkpoints` writes in `service/jobs.py` and `service/monitor.py`. Tests in `tests/test_collectors.py`.
3. **Coverage fields** — add start block, finality, provider and structured missing spans to every job result
   (`service/jobs.py`) and to `covered_ranges` in `service/reports.py`; acceptance criterion 1 in 06.
4. **Access hardening** — `service/app.py`: exchange `?k=` for a cookie on first visit; emailed re-issue (needs a
   provider); `tests/test_service.py` cross-case isolation test; `tests/test_boundaries.py` import check.
5. **Payments** — `service/payments/`: provider adapter interface, Base RPC check of an attested tx hash, x402 wire
   shape (CAIP-2 network, `PAYMENT-RESPONSE`), requirement binding (amount/recipient/asset/network) — all gated on B-4.
6. **Ops** — launchd jobs for staging, backup script using `service.db.backup`, `docs/service/06` runbook updates,
   schema migration helper.
7. **Later phases** — Chainabuse partner API behind authorization records, full native/internal collection, 7702
   history (the type-4 tx that installed the delegation; today only the CURRENT delegate is read), Permit2, NFTs, Postgres migration, partnerships.

## External dependencies (none configured in this phase)

| Dependency | Needed for | Status |
|---|---|---|
| Paid Robinhood Chain RPC (Robinhood docs recommend an Alchemy endpoint — UNSURE on plan/limits) | Monitoring described as active; production screens | Bo's account |
| Checkout provider (e.g. Coinbase Payment Acceptance) | $29 case | Bo's application; eligibility UNSURE |
| x402 facilitator (CDP mainnet or third-party) | Machine screen | Bo's account; Base only |
| Email provider | Alerts, capability-link re-issue | Bo's account |
| Counsel (CA / NV) | Any human-reviewed or assisted-reporting tier | Not engaged |
| Chainabuse partner access | Later submission path | Not requested |
