# 06 — Tests, acceptance criteria, deployment and operations

## Test plan (offline, fixture-driven, like `tests/`)

Baseline: **VERIFIED** `.venv/bin/python -m pytest -q tests` → 80 passed (53 CLI + 27 new) in ~0.4 s, all offline
with fake clients. Lint: `.venv/bin/ruff check service sleuth/collectors tests/test_service.py tests/test_collectors.py tests/test_money.py`
(the pre-existing test files have lint debt and are deliberately not passed to ruff).

| Suite | IMPLEMENTED tests (`tests/test_collectors.py`, `tests/test_money.py`, `tests/test_service.py`) | Still missing |
|---|---|---|
| collectors/logs | windowed scan keeps completed windows + exact missing span on budget expiry; capped window split, other failures → gap | 10k-log split, OR-topic list assertion |
| collectors/receipts | — | everything (collector NOT BUILT) |
| collectors/wallet | ERC-20 vs ERC-721 by shape, Approval/ApprovalForAll, non-standard → gap, screen never clean; `no` only with complete coverage; delegation bypasses cache and survives RPC failure | `transferFrom`-by-allowance wording test |
| collectors/launchcall | decodes exemptions and fee recipient | 31-entry list, unknown selector → gap |
| ring | three-valued verdict incl. incomplete coverage → unknown | — |
| service/queue | exclusive lease + expired reclaim; deep run invisible until settled; enqueue idempotent + attempts exhaust; retry_wait at cap → `failed_terminal`; stale finish rejected; assertions commit with the terminal state (unparseable result → `retry_wait`, never stranded) | crash-after-chunk resume (no chunked commits yet) |
| service/monitoring | reorg rolls back provisional rows and alerts only finalized; degrades on repeated failure keeping cursors; missing hash = provider failure not reorg; alerts per case with stale `sending` reclaim; expired subscriptions end | finalized cursor never rewinds (implied, not asserted); correction alert (NOT BUILT) |
| service/payments | state transitions + duplicate webhook no-op; checkout disabled without receive address; x402 idempotent compute, crash reconcile, payment_id bound to one request, compute failure → 503 with no row, concurrent `computing` → 402; drain checkout → 400 | changed amount/recipient rejected (mock cannot see them); `settlement_pending` on the manual path |
| service/reports | manifest hash verifies and breaks on tamper; three-valued record_result; `dev_ever_sold` `no` needs complete history | golden package, PDT/PST assertion |
| service/isolation | capability token mismatch → 404; end-to-end web flow incl. CSRF 403 | cross-case inference test; `chain_facts` (NOT BUILT) |
| boundaries | — | import-boundary test (payments ⟂ collectors/cases) NOT BUILT; enforced by review only |
| db | online backup + WAL mode | — |

## Acceptance criteria (MVP)

1. Every result names its block range, finality, provider and missing ranges. **FAILING**: results carry head,
   checked-through and a gap list; start block, finality and provider are not fields; monitoring rows do carry hashes.
2. A failed log or receipt call can never produce "no activity", "no selling" or "not linked". **PASSING** for the new
   collectors and for assertions (a `no` needs complete coverage of its stage); the reused CLI modules were already
   fail-closed.
3. Killing the worker after any committed chunk resumes without duplicate evidence. **PARTIAL**: runs commit once at
   the end (lease reclaim re-runs the whole job; evidence keys are unique so no duplicates); monitoring commits per tick.
4. One payment / idempotency key creates one case and one initial run; a settlement timeout creates no second charge
   and no runnable job until reconciled. **PASSING** (x402 path tested; manual path has no timeout state).
5. A reorg supersedes provisional evidence, invalidates unsent alerts, and emits at most one correction. **PARTIAL**:
   provisional rows and pending outbox rows are removed in one transaction; no provisional alerts are sent, so no
   correction exists.
6. Every export matches its manifest hash; the manifest lists chain id, request, versions, RPC identity, covered
   ranges and boundary hashes. **PARTIAL**: hash verifies; request parameters and per-range boundary hashes are
   missing (monitoring cursor hashes are present).
7. The workspace exposes last provisional scan, finalized cursor, lag and provider health at all times. **PASSING**
   (cursors, hashes, lag, last success, last error, state).
8. No public page, submission or outbound message exists without an authorization record. **PASSING** (no such code
   path exists).
9. CLI behaviour and the 53 existing tests are unchanged. **PASSING**.
10. `ruff` on the new files and the full test suite pass before merge. **PASSING** (80 tests).

## Deployment

| Stage | Where | Notes |
|---|---|---|
| Development / staging | Mac mini, `.venv`, `uvicorn` + worker as two launchd jobs, public RPC | No production claims; no customers |
| Production (later, Bo's decision B-5) | Small Linux VM/container, web + worker as separate services, SQLite on local disk | Paid RPC before "monitoring active"; Blockscout secondary only; TLS termination by the host |

## RPC outage behaviour (one hour down)

Cursors unchanged; subscription `degraded`; exponential backoff with jitter; optional failover to a second provider;
catch-up in bounded ranges; coverage gap visible in the workspace; one service-health notice, no false incident
alerts; monitoring window credited.

## Backups

- **VERIFIED** copying only the main `.db` file is not a reliable WAL backup (committed data may be in `-wal`);
  use SQLite's online backup API (`sqlite3.Connection.backup`) or a controlled checkpoint.
- **VERIFIED** Python's bundled SQLite here is 3.45.3; the WAL-reset multi-connection race is fixed in 3.51.3 and
  backports, so keep transactions short, never call RPC inside one, and upgrade before relying on two active writers.
- Nightly backup to a second disk; weekly restore test (PROPOSED; no job exists). Evidence is not append-only today
  (reorg deletes provisional rows), so a restore can resurrect since-orphaned provisional evidence; finalized rows are
  never deleted.

## Cost (see 04 for the table)

Fixed ~$50–200/month at production; per-case variable cost is cents unless a trace/indexing provider becomes
necessary for drain cases.
