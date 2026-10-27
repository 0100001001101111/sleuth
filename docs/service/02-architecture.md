# 02 — Architecture and data model

## Implemented status (2026-10-26, branch `feat/incident-service`)

Offline tests: `tests/test_service.py` (27), `tests/test_collectors.py` (8) and `tests/test_drain.py` (21) on top of the
61 CLI tests — 117 pass in about 0.6 s with `.venv/bin/python -m pytest -q tests`. Live smoke (STOX rug screen → partial in 4.9 s with one explorer
gap; dev-wallet drain screen → succeeded in 0.3 s, delegation observed) was run by hand on 2026-10-26; no artifact is
committed. The one Codex code review of this code (25 findings, all applied) ran in a read-only sandbox and could not
execute tests.

| Component | Path | Status | Notes |
|---|---|---|---|
| Collectors: windowed topic scan, wallet drain screen, `launchAndBuy` calldata decode, early-buy concentration rule | `sleuth/collectors/{logs,wallet,launchcall,ring}.py` | IMPLEMENTED | Partial results with coverage + typed gaps; 30k-block windows; a budget deadline returns the completed windows plus the exact missing span. CLI untouched |
| Drain investigation (rule `drain-investigation/0.1`): multi-chain presence, current EIP-7702 delegation with delegate identity, outflow inventory with a poisoning quarantine (look-alike symbols, fake native-coin tokens, canonical-symbol impersonators, scam-flagged submitters, zero-value, look-alike counterparties), per-transaction mechanism (`eip7702_sweep`, `call_into_delegated_account`, allowance / Permit2 / permit use, key-signed rows tied to a sweep by recipient or timing; a third-party pull that follows the wallet's own exact-amount approval within an hour is listed as self-authorised and not counted as loss; a canonical asset sent to a look-alike or scam-flagged address is the loss, not noise; third-party rows more than an hour before the reported time are listed with their own total and not summed as the loss, with a prompt to re-run with an earlier time), victim protocol/approval profile, suspect profiles (type-4 authorities, delegates with code hash, outbound by label kind: exchange / bridge / privacy pool / DEX, first funding, swept-asset balances), suspect relations; next hops (a suspect's rehearsal sends of a swept asset point at the address it will pay; each such address is read back to its first funding with explorer labels, its exchange / bridge counterparties, contact back to the suspect and what it forwarded — the protocol in 12); ERC-4337 bundler submitters treated as services; verified delegate source parsed for a hard-coded receiver; single tx explainer | `sleuth/collectors/drain.py` | IMPLEMENTED (beta) | Three-valued verdicts per question; partial results with typed gaps (`truncated`, `capped`, `budget`, `provider`); explorer reads fan out in threads and half the budget is kept back for suspect profiling; the delegation check runs on every chain the explorer knows the address on, and a live delegate that equals the delegate installed by a third-party sweep is reported as "set in the loss transaction" whatever its name (a verified vendor label does not make it safe); the case page and the plain-words panel name every live delegation per chain and state that revoking token approvals does not clear one; suspects keep their role (submitter / recipient) and outbound rows are counted from the first loss on; a chain skipped for time, budget or a provider failure turns every negative verdict into unknown; the case chain then the busiest other chain, four strongest-linked suspects, 40 loss txs, 8 type-4 txs per suspect. Tested offline against a fake explorer (`tests/test_drain.py`) and live by hand on 2026-10-27 |
| Receipt collector with hash-list checkpoints | — | NOT BUILT | Dev-history and trace receipt loops still discard work on budget expiry (design record entry 8) |
| SQLite WAL persistence + online backup | `service/db.py` | IMPLEMENTED | Tables: cases, investigation_runs, coverage_checkpoints (created, never written), evidence_items, assertions, monitoring_subscriptions, notification_outbox, payments, x402_replays, audit_events. No migration tool: `PRAGMA user_version` must equal `db.SCHEMA_VERSION` or `connect()` refuses the file; a schema edit means bumping it and deleting the scratch `service.db`. `chain_facts`, `entitlements`, `authorizations`, `review_decisions`, `report_packages`, `submissions`, `responses`: NOT BUILT |
| Queue: BEGIN IMMEDIATE lease, heartbeat, expired-lease reclaim, attempts cap → `failed_terminal`, entitlement gate, lease-checked finish | `service/queue.py` | IMPLEMENTED | `partial` is terminal; deep runs are invisible to the worker until a payment is `settled`/`entitled`; `finish()` runs the caller's `after` hook inside the same transaction as the state change |
| Jobs: `rug_screen` (triage + calldata + ring rule), `money_trail` (free follow-up enqueued by `cases.follow_up_tx` inside the worker's finish transaction, so it exists iff the screen's terminal state committed), `rug_deep` (dev-history + trace), `drain_screen`, `drain_investigation` (free follow-up enqueued after every drain screen the same way), `tx_explain` | `service/jobs.py` | PARTIAL | Budgets 20 s / 150 s / 240 s / 20 s / 200 s / 20 s (the queue lease is 300 s with a heartbeat); provider failures become gaps, never exceptions. `rug_*` still call the CLI modules `triage`, `devhistory`, `trace`, which write `data/watchlist.json` and discard partial receipt work on budget expiry |
| Case lifecycle, capability token, three-valued assertions | `service/cases.py` | IMPLEMENTED | Token sha256 at rest, constant-time compare; brakes: 3 open screens per subject per hour, 60 cases per hour service-wide, 50 queued runs; a `no` verdict requires complete coverage of the stage it came from |
| Monitoring: provisional + finalized cursors with block hashes, reorg rollback to the common ancestor, degraded state, finalized-only alerts, expired subscriptions ended | `service/monitor.py` | IMPLEMENTED | Transfer logs only (the token's Transfer topic × tracked addresses); unavailable hash = provider failure, never a reorg; boundary hash re-checked after a partial scan; `REORG_DEPTH_MAX` 512; no RPC inside a transaction |
| Notification outbox: claim/reclaim, per-case dedup, file/JSONL sender | `service/notify.py` | PARTIAL | `email`/`sms` channels fall through to the local JSONL file; no provider is wired |
| Evidence package JSON + HTML with recomputable manifest sha256 | `service/reports.py` | IMPLEMENTED | Manifest contents listed under "Report manifest" below; narrower than the design |
| Payments: state machine, manual USDC-on-Base order with operator attestation, x402-style `exact` gate with a mock facilitator | `service/payments/{base,manual,x402}.py` | PARTIAL | Flag `SLEUTH_X402=1`; receive address from `SLEUTH_PAY_TO_BASE_USDC`; no keys anywhere; no provider adapter, no on-chain check of the attested tx, pilot wire shape (see 04) |
| Plain-language layer: question / meaning / gap sentences per predicate, case summary, incident-time form parsing | `service/plain.py` | IMPLEMENTED | Jinja filters `question`, `meaning`, `plain_gap`; `summary()` only restates recorded verdicts and returns `[]` on any error; `incident_time()` parses the dropdown (`when` 6/24 → `hours_after`, exact `datetime-local` in America/Los_Angeles); the HTML export in `service/reports.py` still prints raw predicate keys |
| Web: `/`, `/screen`, `/drain`, `/tx` (transaction explainer, case kind `tx`), `/c/{id}?k=`, `/c/{id}/export.{json,html}`, `/c/{id}/checkout`, `/methodology`, `/corrections`, `/admin/payments` (Basic auth + per-process CSRF token + lockout), `/v1/screen/token`, `/healthz` | `service/app.py`, `service/templates/` | IMPLEMENTED | Server-rendered Jinja; no public case pages; `Referrer-Policy: no-referrer`, `Cache-Control: no-store`; per-IP brakes (10 screens/h, 5 failed admin logins/10 min); connections per request thread |
| Worker + entry point | `service/worker.py`, `service/__main__.py` | IMPLEMENTED | `python -m service` runs web + worker in one process on 127.0.0.1:8400 (`SLEUTH_HOST`/`SLEUTH_PORT`) |

DEFERRED / NOT BUILT: real facilitator (CDP) and a checkout provider, email/SMS providers, Postgres swap, `chain_facts`
dedup across cases, authorizations/review/submission tables and any external reporting path, full (unpaginated)
native/internal history, historical 7702 index, Permit2/ERC-1155/NFT, cross-chain, TLS/reverse proxy, launchd/systemd units, retention
job, capability-token cookie exchange (the token travels in `?k=`), emailed link re-issue, request-time RPC check of a
manually attested Base tx.

Everything below this line is the design as agreed in the three rounds, annotated where the build differs; where a
design paragraph and the table above disagree, the table is what exists.

## Repo reuse and refactor findings

| Existing module | Reused by the service as | Refactor needed? |
|---|---|---|
| `sleuth/client.py` (`Client`, budgets, batched RPC, cache) | Every collector and job; `errors=` side channel used per call | None for the pilot; `BudgetExceeded` from `client.rpc()` still discards the whole batch (collectors avoid it by windowing) |
| `sleuth/onchain.py` (`TRANSFER`, `pad`, `transfer_logs`) | Constants and padding in collectors and monitor | None; `transfer_logs` itself is not used by the new scanner |
| `sleuth/triage.py` | `rug_screen` (launch, flow, tracked sellers) | YES (later): it saves the watchlist and reads `client._pool`; needs a library mode that returns without side effects |
| `sleuth/devhistory.py`, `sleuth/trace.py` | `rug_deep` | YES (later): partial returns on budget instead of dropping a wallet or receipt batch |
| `sleuth/launch.py`, `sleuth/txdecode.py`, `sleuth/labels.py`, `sleuth/flows.py` | Calldata decode, DEAD/zero sets, netting | YES (later): `flows.detect_pools` calls a two-way token counterparty a pool without any quote-asset evidence, so a game/distributor contract (RAID 0x05a3a6…) made the screen say the launcher "sold" 569M tokens it had in fact parked; the money trail corrects this per disposal, triage itself should require quote flow |
| `sleuth/collectors/money.py` (`money_trail`, rule `money-trail/0.2`: credits must be ERC-20/native in the same tx; only credited assets followed, FIFO up to the proceeds; exit = configured `known_services` only; truncated pages => `unverified`) | `money_trail` job; explorer pages `/transactions`, `/internal-transactions`, `/token-transfers` via `client.paginate`, labels via `labels.classify` | None; depth/fan-out/page caps and `BudgetExceeded` become gaps and `None` verdicts |
| `sleuth/report.py` (`local`, `short`) | Timestamp display (UTC + America/Los_Angeles with PDT/PST) in templates and exports | None |
| `sleuth/cli.py` | Not imported | None; CLI contracts unchanged, 53 original tests untouched |


## Shape

```
browser / API client
      │
FastAPI web process (Jinja server-rendered pages; HTMX optional)
      │
SQLite (WAL) behind service/db.py   ──  one file, online-backup API
      │
single worker process (investigation runs + monitoring ticks)
      │
Robinhood Chain RPC (public now; paid provider before "monitoring active")  +  Blockscout (secondary only)
```

- One host. Web and worker are separate processes sharing one SQLite file in WAL mode. **VERIFIED** SQLite allows
  concurrent readers and one writer; all processes must be on the same host; the bundled SQLite here is 3.45.3,
  below the 3.51.3 WAL-reset advisory fix, so transactions stay short and RPC calls never happen inside one.
- Postgres only on a measured trigger: recurring `SQLITE_BUSY`, p95 write-acquire above 250 ms, or a second host.
- Payment code lives in `service/payments/` and is never imported by collectors. No signing, custody or wallet code
  anywhere in the repo.
- The CLI (`sleuth/cli.py` and its modules) is untouched; the service imports `flows`, `labels`, `launch`,
  `txdecode`, `burn`, `devhistory`, `devprofile`, `trace`, `triage` as libraries.

## Collector contract (`sleuth/collectors/`)

Codex R1 finding (VERIFIED): today `Client.errors` is a shared side channel, triage reaches into `client._pool`,
`transfer_logs` and `client.rpc()` discard completed spans/batches on `BudgetExceeded`, `trace.wallet` drops a whole
wallet on timeout, and triage/dev-history mutate the global watchlist during analysis. The collectors package applies
the contract below to the NEW collection paths (topic scanner, wallet events, delegation, calldata, ring rule) without
changing those modules. It does not yet wrap `triage`, `devhistory` or `trace`: the rug jobs still inherit their
watchlist writes and budget-expiry losses (PARTIAL; backlog item "Replayability refactors" in 07).

```
Result(items, coverage, gaps, source_attempts, next_checkpoint)
Coverage(requested_span, completed_spans, missing_spans, head_number, head_hash, finality, complete)
Gap(kind, span | hashes, reason)          # "rpc_cap", "budget", "explorer", "unsupported"
```

Rules:
1. Topic-only `eth_getLogs` runs in 30,000-block windows (**VERIFIED** RPC cap); OR-lists in `topics[0]` combine
   Transfer/Approval/ApprovalForAll in one call per window (**VERIFIED**).
2. Each completed log window is kept and the exact missing span is reported when the deadline hits (IMPLEMENTED in
   `logs.scan_topics`; the `coverage_checkpoints` table exists but nothing writes it yet). Receipt-batch hash-list
   checkpoints: NOT BUILT, so receipt loops in the reused CLI modules can still lose collected work.
3. Every item carries `source` (rpc | blockscout | cache), `observed_at`, `block_number`, `block_hash`, `finality`
   and `collector_version`.
4. Delegation checks bypass the 6-hour EOA code cache and keep the delegate target from `0xef0100 || address`.
5. Launch calldata decoding reads `launchAndBuy(...)` on the verified PonsV2LaunchAndBuy contract: waiver list,
   `creatorFeeRecipient`, `creatorTaxBps`, name/symbol, `quoteIn` (**VERIFIED** field layout by live decode).
6. Validation errors raise typed exceptions; provider failures return partial results.

## Data model (SQLite tables)

| Table | Core fields | Visibility |
|---|---|---|
| `cases` | id (capability token hash), type (rug / drain / tx), chain_id, subject address, email hash, status, created/closed, retention | Private |
| `assertions` | case, subject, predicate, object, verdict (yes / no / unknown), reason, rule + version, scope, evidence refs; user claims flagged `source=user` | Private |

**Actual schema (IMPLEMENTED, `service/db.py`) where it differs from the design table:** `cases.id` is a random UUID
and the capability token's sha256 is a separate `access_hash` column; `cases.email` is stored **as given** (it doubles
as the alert destination), not hashed; the user's `claimed_loss` is a raw text column on `cases`, shown as "reported",
and there is no narrative column and no `source` column on `assertions`; `evidence_items.id` is a random UUID with a
separate `content_hash`, keyed `(case_id, key)`, and provisional rows are **deleted** on reorg and **updated** to
`finalized` on promotion, so evidence is not append-only; `audit_events` carries a free-form `detail_json`, not
before/after hashes; `monitoring_subscriptions` uses `provisional_cursor/provisional_hash` and
`finalized_cursor/finalized_hash` (the design's `evidence_cursor`); `notification_outbox` dedups on
`UNIQUE (case_id, dedup_key)` with a `claimed_at` for the sender claim; `payments` has `UNIQUE (idempotency_key)` and a
partial unique index on `(network, tx_hash)`; `chain_facts`, `entitlements` and the reporting tables are not created.
| `investigation_runs` | case, kind, state (queued / leased / retry_wait / succeeded / partial / failed_terminal / canceled), input_hash, collector_version, budgets, lease (`leased_by`, `lease_expires_at`, `heartbeat_at`), attempts, timings | Private operational |
| `coverage_checkpoints` | run or subscription, collector, start/end/next block, boundary block hash, finality, status, gaps JSON | Private operational; summarized to the customer |
| `evidence_items` | immutable id = sha256 of canonical content, type, source, observed_at, chain/block/block_hash/tx/log_index, finality, run; keyed `(chain_id, tx_hash, log_index, fact_type)` | Private link to a shared fact |
| `chain_facts` | chain, fact type, canonical key, canonical JSON, block hash, finality | Shared across cases; contains no case, customer, narrative or victim-role data; no global lookup endpoint |
| `monitoring_subscriptions` | case, scope (addresses + roles + token), starts/ends, state (active / degraded / ended), provisional_cursor, evidence_cursor, cursor block hashes, last_success, lag | Private |
| `notification_outbox` | case, dedup key (includes canonical block hash), channel, destination ref, payload hash, state, attempts | Private operational |
| `payments` | logical order, provider, method, payment chain, asset, amount, payer ref, provider id, tx hash, state, idempotency key; unique `(provider, idempotency_key)`, unique settled `(network, tx_hash, transfer_index)` | Financial / private |
| `entitlements` | order/case, product, starts/ends, status, source payment | Private |
| `audit_events` | actor, action, target, request id, ip hash, before/after hashes, time | Security-restricted |
| `authorizations`, `review_decisions`, `report_packages`, `submissions`, `responses` | Per Codex R1 data model | Schema reserved; no submission code path in MVP |

UUID ids, UTC epoch timestamps, JSON only for irregular payloads, explicit version columns. Append-only evidence is a
design goal, not the current behaviour (see above).

## Queue semantics (SQLite)

Claim with `BEGIN IMMEDIATE` + conditional lease update (the SQLite equivalent of `SKIP LOCKED`); heartbeat; expired
leases reclaimed; unique logical key `(case_id, run_kind, input_hash, collector_version)`; each completed chunk commits
evidence + checkpoint + lease in one short transaction; `partial` is a first-class terminal state; the worker refuses
to lease a paid job until its entitlement is settled.

## Monitoring: two cursors and reorgs

- **VERIFIED** The RPC serves `latest`, `safe` and `finalized`; measured lag ≈ 9.3 min (safe) and ≈ 15.9 min
  (finalized) behind latest at head ≈ 98.81M; `eth_getLogs` accepts `safe` as a block tag.
- `provisional_cursor` scans to `latest` (evidence rows marked provisional; **no alerts** are sent from them);
  `finalized_cursor` scans to `finalized` and is the only source of notifications. Because no provisional alert ever
  goes out, the "explicit correction" alert is not needed yet and is NOT BUILT.
- Reorg procedure (Codex R3, accepted): store block number + hash at every checkpoint; re-fetch the boundary hash
  before advancing; on mismatch walk back to the common ancestor; in one transaction mark provisional facts from
  orphaned blocks superseded and invalidate unsent outbox rows derived from them; dedup keys include the canonical
  block hash so a replacement alert is not suppressed; re-run the range; emit one explicit correction if a
  provisional alert already went out; never rewind a finalized checkpoint.
- Provider outage: cursor unchanged, subscription `degraded`, backoff with jitter, one service-health notice,
  catch-up in bounded ranges, coverage gap shown in the workspace, monitoring window credited.

## Report manifest (Codex R3, accepted)

Design: `manifest_hash = sha256(canonical JSON of the package excluding the hash field)`, including chain id, request
parameters, collector and rule versions, RPC/explorer identity, covered block ranges with boundary hashes and finality,
generation time (UTC), display timezone, and the hash of every artifact.

IMPLEMENTED (`service/reports.py`): the sha256 is computed over the canonical JSON of the whole package with
`manifest.sha256` blank, and `verify()` recomputes it. The manifest today contains `chain_id`, `rpc`,
`collector_versions`, `rule_versions`, `covered_ranges` (per run: `collector`, `checked_through`, `head`, `complete`),
`monitoring_cursors` (per subscription: `finalized_cursor`, `finalized_hash`, `provisional_cursor`,
`provisional_hash`), `generated_at_utc` and `sha256`. NOT yet in the manifest: request parameters, start blocks,
finality and provider per range, missing ranges as structured spans, per-artifact hashes. `display_tz` is attached
after hashing and excluded from verification, so the same package can be re-rendered in another zone. Raw amounts are
kept as decimal strings in evidence payloads.

## Deployment

Staging on the Mac mini (no production claims). Production: small always-on Linux VM or container, web + worker as
separate services, SQLite file on local disk with scheduled online backups, a paid RPC provider before monitoring is
described as active, Blockscout as secondary source only.
