# Round 1 technical verdict

## Executive verdict

**VERIFIED —** Sleuth contains a credible deterministic analysis kernel, especially transaction-level token-flow netting, fail-closed log retrieval, and three-valued verdict logic. The 53 offline tests pass in the current environment.

**VERIFIED —** It is not yet a safe service library. Collection, mutable case state, deadlines, caching, presentation, and CLI side effects are intertwined. Some data described as “durable” is cached before finality or is actually mutable.

**INFERRED —** Do not launch both “rug” and “wallet drained.” The first paid MVP should be Robinhood Chain token-rug investigation plus narrowly defined monitoring. Wallet-drain support should remain beta until native-value and EIP-7702 coverage can meet the same evidence standard.

**INFERRED —** Use FastAPI + Supabase Postgres + one worker + server-rendered HTML/HTMX. Do not add Next.js, Redis, Celery, LLM-generated conclusions, automated external submissions, or x402 to the first release.

---

## 1. Repository verdict

### Reusable now

| Status | Component | Verdict |
|---|---|---|
| VERIFIED | `flows.trades`, `flows.aggregate` | Reuse essentially as-is. They are pure deterministic transforms with per-transaction netting, routed-flow reachability, mint handling, and cautious burn attribution. [flows.py](/Users/owner/sleuth/sleuth/flows.py:88) |
| VERIFIED | `flows.detect_pools` | Reuse as a versioned heuristic, not as ground truth. It cleanly separates one-way sinks and optional swap evidence, but two-way flow remains heuristic. [flows.py](/Users/owner/sleuth/sleuth/flows.py:26) |
| VERIFIED | `labels.classify` | Reuse as a policy function if its inputs and rule version are recorded. It correctly separates service addresses from ownership evidence. [labels.py](/Users/owner/sleuth/sleuth/labels.py:5) |
| VERIFIED | Log normalization and splitting | Reuse the algorithm, not the present API. It recursively splits on the 10M-block limit and 10,000-log cap and refuses silent truncation. [onchain.py](/Users/owner/sleuth/sleuth/onchain.py:29) |
| VERIFIED | Receipt-level token extraction | Reuse after generalizing beyond a single ERC-20. Receipt caching and topic-emitter collection are useful primitives. [onchain.py](/Users/owner/sleuth/sleuth/onchain.py:310) |
| VERIFIED | Block-time lookup | Reuse with explicit provenance. It validates returned block height before caching timestamps. [onchain.py](/Users/owner/sleuth/sleuth/onchain.py:153) |
| VERIFIED | Three-valued verdict rules | Reuse. Triage, trace, and dev-history generally preserve `true / false / unknown` rather than converting incomplete evidence into a negative. [triage.py](/Users/owner/sleuth/sleuth/triage.py:69), [devhistory.py](/Users/owner/sleuth/sleuth/devhistory.py:26), [trace.py](/Users/owner/sleuth/sleuth/trace.py:253) |
| VERIFIED | Launch-event collector | Reuse as chain-specific plugin logic. It records whether a launcher came from a launch event or only a candidate creation signer. [launch.py](/Users/owner/sleuth/sleuth/launch.py:12) |

### Must be refactored behind a stable collector interface

**VERIFIED — `Client` mixes too many responsibilities.** It owns transport, retry policy, deadline, rate limiting, cache location, mutable errors, request accounting, and a private thread pool. [client.py](/Users/owner/sleuth/sleuth/client.py:52)

Specific problems:

- **VERIFIED —** `client.errors` is a shared mutable side channel. Callers compare its length to infer whether a receipt operation failed. Concurrent tasks can make that attribution unreliable. [client.py](/Users/owner/sleuth/sleuth/client.py:61), [devhistory.py](/Users/owner/sleuth/sleuth/devhistory.py:113)
- **VERIFIED —** Triage reaches through the abstraction and submits work directly to `client._pool`. [triage.py](/Users/owner/sleuth/sleuth/triage.py:187), [triage.py](/Users/owner/sleuth/sleuth/triage.py:220)
- **VERIFIED —** `Client` creates its own executor while triage and trace create additional executors. Concurrency and cancellation cannot be governed per job. [client.py](/Users/owner/sleuth/sleuth/client.py:64), [triage.py](/Users/owner/sleuth/sleuth/triage.py:118), [trace.py](/Users/owner/sleuth/sleuth/trace.py:165)
- **VERIFIED —** `close(wait=False)` does not establish that background requests have stopped before a service job is finalized. [client.py](/Users/owner/sleuth/sleuth/client.py:347)
- **VERIFIED —** Responses are untyped dictionaries with no schema or collector-version identifier.
- **INFERRED —** Replace the side channel with a returned `CollectionResult` containing `items`, `coverage`, `gaps`, `source_attempts`, and `checkpoint`.

**VERIFIED — configuration is process-oriented.** `load()` raises `SystemExit` and creates directories. `chain()` also raises `SystemExit`. Those are CLI semantics and inappropriate inside an API or worker. [config.py](/Users/owner/sleuth/sleuth/config.py:61), [config.py](/Users/owner/sleuth/sleuth/config.py:79)

**VERIFIED — `Watchlist` is not a service repository.** Every instance reads the entire global JSON file and atomically replaces it on save; concurrent processes can overwrite each other’s changes. `last_block` exists but has no monitoring loop. [watchlist.py](/Users/owner/sleuth/sleuth/watchlist.py:5), [watchlist.py](/Users/owner/sleuth/sleuth/watchlist.py:17), [watchlist.py](/Users/owner/sleuth/sleuth/watchlist.py:44)

**VERIFIED — the SQLite cursor table is unused.** It is created and has accessors, but current collection code does not use it to resume ranges. [client.py](/Users/owner/sleuth/sleuth/client.py:27), [client.py](/Users/owner/sleuth/sleuth/client.py:43)

**VERIFIED — orchestration currently mutates persistent state.** `triage.run` and `devhistory.run` read and save the watchlist; `trace` mutates it from the CLI. That prevents replaying a run as a pure calculation. [triage.py](/Users/owner/sleuth/sleuth/triage.py:115), [triage.py](/Users/owner/sleuth/sleuth/triage.py:449), [devhistory.py](/Users/owner/sleuth/sleuth/devhistory.py:53), [cli.py](/Users/owner/sleuth/sleuth/cli.py:189)

### The cache is less durable than advertised

**VERIFIED —** `Cache.get` internally distinguishes cached values through metadata, but most public methods discard that metadata. [client.py](/Users/owner/sleuth/sleuth/client.py:33), [client.py](/Users/owner/sleuth/sleuth/client.py:269)

**VERIFIED —** `token()` silently combines cached identity with live supply and holder counts. The output does not identify which fields were cached. [client.py](/Users/owner/sleuth/sleuth/client.py:310)

**VERIFIED —** Balances are live; block timestamps and receipts may be cached; callers are not given field-level provenance. [onchain.py](/Users/owner/sleuth/sleuth/onchain.py:153), [onchain.py](/Users/owner/sleuth/sleuth/onchain.py:274), [onchain.py](/Users/owner/sleuth/sleuth/onchain.py:319)

**VERIFIED —** Transaction bodies and receipts are cached as soon as they are returned. The code does not require `safe` or `finalized` status and does not store a canonical block hash for later reorg invalidation. [client.py](/Users/owner/sleuth/sleuth/client.py:327), [onchain.py](/Users/owner/sleuth/sleuth/onchain.py:325)

**VERIFIED —** Address “identity” is cached indefinitely even though verification status, tags, scam flags, proxy implementation, and names can change. [client.py](/Users/owner/sleuth/sleuth/client.py:280)

**VERIFIED —** `codes()` caches an EOA result for six hours. For incident response, that can hide a newly installed EIP-7702 delegation for up to six hours. It also stores only `eoa7702`, discarding the delegate target embedded after `0xef0100`. [onchain.py](/Users/owner/sleuth/sleuth/onchain.py:247)

**INFERRED —** Service artifacts need explicit fields such as:

```python
source: Literal["rpc", "blockscout", "cache"]
observed_at: datetime
cache_hit: bool
block_number: int | None
block_hash: str | None
finality: Literal["latest", "safe", "finalized", "unknown"]
collector_version: str
```

### Where deadlines erase partial findings

| Status | Path | Actual behavior |
|---|---|---|
| VERIFIED | `transfer_logs` | It accumulates split spans in a local list. A later `BudgetExceeded` throws the whole call away; completed spans and the next checkpoint are not returned. [onchain.py](/Users/owner/sleuth/sleuth/onchain.py:36) |
| VERIFIED | Triage before flow stage | `triage.run` re-raises if `out["flow"]` is absent. The CLI converts that to `SystemExit`, so no evidence file is written. [triage.py](/Users/owner/sleuth/sleuth/triage.py:80), [cli.py](/Users/owner/sleuth/sleuth/cli.py:166) |
| VERIFIED | Triage seller loop | Earlier sellers survive, but a seller whose acquisition scan times out disappears from that enrichment stage. The loop breaks after a generic gap. [triage.py](/Users/owner/sleuth/sleuth/triage.py:273) |
| VERIFIED | Triage outer partial | After flow exists, the outer catch returns a partial run, but it does not persist watchlist mutations reached before the normal final `save()`. [triage.py](/Users/owner/sleuth/sleuth/triage.py:86), [triage.py](/Users/owner/sleuth/sleuth/triage.py:449) |
| VERIFIED | `trace.wallet` | A timeout at any point discards that wallet’s entire partially built dictionary. `trace.group` preserves other wallets and adds a gap. [trace.py](/Users/owner/sleuth/sleuth/trace.py:39), [trace.py](/Users/owner/sleuth/sleuth/trace.py:165) |
| VERIFIED | Trace executor shutdown | The executor context waits for submitted futures on exit. The nominal budget can therefore be exceeded while threads finish. [trace.py](/Users/owner/sleuth/sleuth/trace.py:165) |
| VERIFIED | Dev-history | The current wallet is appended before its scans. A deadline may leave it present with `complete=True` even though its fields are only partly populated; the overall verdict is safely forced to `true` or `unknown`. [devhistory.py](/Users/owner/sleuth/sleuth/devhistory.py:14), [devhistory.py](/Users/owner/sleuth/sleuth/devhistory.py:93) |
| VERIFIED | Burn/tx/contract | These commands have no command-specific `BudgetExceeded` recovery. A deadline can prevent `_save()` and produce no run artifact. [cli.py](/Users/owner/sleuth/sleuth/cli.py:164), [cli.py](/Users/owner/sleuth/sleuth/cli.py:225) |

**INFERRED —** Every range collector should checkpoint after each completed span. Deadlines should stop scheduling new work, return completed facts, and mark the exact uncollected range—never discard already collected evidence.

### Keep CLI-only

**INFERRED —** Keep these outside the service core:

- Argument parsing, address validation, `_save`, and text output in [cli.py](/Users/owner/sleuth/sleuth/cli.py:24).
- Markdown terminal reports in [report.py](/Users/owner/sleuth/sleuth/report.py:20).
- File-based `enrich` and `watch` commands.
- `scripts/pons_scan.py`.
- The current `contract.audit` until it reports pagination and source completeness; it currently reads only one recent transaction page. [contract.py](/Users/owner/sleuth/sleuth/contract.py:7)

---

## 2. Wallet-drain collection without archive or trace APIs

### ERC-20 and ERC-721 outflows across all contracts

**VERIFIED —** Standard JSON-RPC permits `eth_getLogs` with `topics` and no contract `address`, so the filter can span every emitting contract. Robinhood documents its endpoint as standard JSON-RPC. [Ethereum RPC specification](https://eips.ethereum.org/EIPS/eip-1474), [Robinhood connection documentation](https://docs.robinhood.com/chain/connecting/)

**UNSURE —** I did not live-probe Robinhood’s public endpoint from this restricted environment. The repo has measured a 10M-block query ceiling and 10,000-log cap, but topic-only performance on that endpoint still needs a production probe.

**INFERRED —** Query:

```json
{
  "fromBlock": "...",
  "toBlock": "...",
  "topics": [
    "keccak256(Transfer(address,address,uint256))",
    "0x000000000000000000000000<VictimAddress>"
  ]
}
```

That matches `from == victim` for both ERC-20 and ERC-721. Distinguish them by shape:

- ERC-20: three topics; amount in `data`.
- ERC-721: four topics; token ID in `topics[3]`.

The standards define those event layouts. [ERC-20](https://eips.ethereum.org/EIPS/eip-20), [ERC-721](https://eips.ethereum.org/EIPS/eip-721)

**VERIFIED —** Current `transfer_logs` cannot do this: it always sets a token contract address and deliberately accepts only three-topic ERC-20-shaped logs. [onchain.py](/Users/owner/sleuth/sleuth/onchain.py:29)

**INFERRED —** Base query cost is approximately:

```text
ceil((to_block - from_block + 1) / 9,999,001)
```

per event filter, plus recursive splits if a result hits 10,000 logs. Filtering by an indexed victim should make result counts small, but the provider may still charge these as expensive broad-history queries.

**INFERRED —** Add ERC-1155 before claiming “all NFTs.” ERC-1155 uses `TransferSingle` and `TransferBatch`, not the ERC-20/721 `Transfer` signature. [ERC-1155](https://eips.ethereum.org/EIPS/eip-1155)

### Approval history

**INFERRED —** Run topic-only filters for:

```text
Approval(address,address,uint256), topics[1] = victim
ApprovalForAll(address,address,bool), topics[1] = victim
```

ERC-20 and ERC-721 share the `Approval` signature but have different event shapes. `ApprovalForAll` covers ERC-721 and ERC-1155 operator grants.

**INFERRED —** Preserve every grant and revocation chronologically. At incident time, report the event-derived state; for current exposure, confirm with live `allowance`, `getApproved`, or `isApprovedForAll` calls. Without archive state, historical current-allowance calls are unavailable.

**INFERRED —** Treat non-standard or silent token implementations as an explicit coverage gap.

### EIP-7702

**VERIFIED —** The present code detects the `0xef0100` prefix but only returns `eoa7702`. [onchain.py](/Users/owner/sleuth/sleuth/onchain.py:250)

**INFERRED —** The collector must retain:

```text
delegated = code starts with 0xef0100
delegate_target = code[3:23]
raw_code
observed_block
observed_block_hash
```

EIP-7702 defines the delegation indicator as `0xef0100 || address` and places authorization tuples in type-4 transactions. A delegation change persists even if the transaction’s execution later reverts. [EIP-7702](https://eips.ethereum.org/EIPS/eip-7702)

**INFERRED —** Bypass the six-hour EOA cache for incident collection and monitoring. Inspect known incident transactions for `authorizationList`, recover each authority from its signed tuple, and record whether the victim authorized the delegate.

**UNSURE —** Discovering every historical type-4 transaction that authorized a victim is not feasible from ordinary topic scans because authorization changes emit no log. It requires explorer/indexer support, transaction enumeration, or a custom index.

### Native ETH drains

**VERIFIED —** Top-level transactions and explorer-indexed internal transfers are already used separately. `trace.wallet` reads address transactions and internal transactions, while `txdecode` marks internal ETH unavailable when Blockscout fails. [trace.py](/Users/owner/sleuth/sleuth/trace.py:98), [txdecode.py](/Users/owner/sleuth/sleuth/txdecode.py:84)

**INFERRED —** Detection tiers should be:

1. Full paginated Blockscout address transactions for direct `from=victim`, verified against RPC transaction and receipt.
2. Full paginated Blockscout internal transactions for value calls originating from the victim or delegated victim.
3. A paid trace/indexing provider or self-operated trace-capable node as the production fallback.

**VERIFIED —** Balance differences are not an adequate substitute. They cannot identify internal recipients, require historical state for exact block comparisons, and conflate transfers with gas.

**INFERRED —** If the incident transaction is already known, its top-level `value` is recoverable from ordinary RPC. Native calls made inside delegated EIP-7702 execution are not recoverable from receipts alone.

### Permit and Permit2

**VERIFIED —** The brief’s “Permit has no Approval event” premise is wrong for EIP-2612. A compliant successful `permit` must emit the corresponding ERC-20 `Approval`. [EIP-2612](https://eips.ethereum.org/EIPS/eip-2612)

**VERIFIED —** Permit2 has two different cases:

- `AllowanceTransfer` stores an allowance and emits Permit2’s own `Permit` event.
- `SignatureTransfer` is a one-transaction authorization that bypasses a standing allowance; the resulting token transfer is visible, but there need not be a standing approval event.

[Permit2 AllowanceTransfer interface](https://github.com/Uniswap/permit2/blob/main/src/interfaces/IAllowanceTransfer.sol), [Permit2 architecture](https://github.com/Uniswap/permit2)

**INFERRED —** Recoverable evidence:

- Token-native EIP-2612: token `Approval` log and decodable permit calldata.
- Permit2 allowance: Permit2 `Permit` event, plus the token’s earlier approval to Permit2.
- Permit2 signature transfer: token `Transfer` log and, when exposed, the signed parameters in transaction calldata.
- Known routers/multicalls: recursively decode embedded calldata by ABI.
- Unknown internal calls: receipt proves asset movement, but without a trace the exact internal caller and signed payload may remain unknown.
- An unused off-chain signature is unrecoverable from chain data.

**UNSURE —** Do not assume canonical Permit2 is deployed on Robinhood Chain. Check `eth_getCode` and chain-specific deployment records first.

### Sweeper-bot detection

**INFERRED —** Report a “sweeper pattern,” not “bot proven,” when several of these co-occur:

- Incoming native or token value leaves in the same or next block.
- Repeated destination or controller across multiple assets.
- Near-total balance depletion after each credit.
- Repeated gas sponsorship from the same funder.
- Transfers continue after the victim stops interacting.
- A newly installed or changed EIP-7702 delegate precedes the sweeps.
- Several victim wallets converge on the same collection address.

### Proposed collector API

**INFERRED —** Add a typed, checkpointable package:

```python
# sleuth/collectors/types.py
@dataclass(frozen=True)
class BlockSpan:
    start: int
    end: int
    finality: Literal["latest", "safe", "finalized"]

@dataclass(frozen=True)
class Coverage:
    requested: BlockSpan
    completed_spans: tuple[BlockSpan, ...]
    missing_spans: tuple[BlockSpan, ...]
    head_number: int
    head_hash: str
    complete: bool

@dataclass(frozen=True)
class CollectionResult[T]:
    items: tuple[T, ...]
    coverage: Coverage
    gaps: tuple[EvidenceGap, ...]
    source_attempts: tuple[SourceAttempt, ...]
    next_checkpoint: Checkpoint | None

def scan_logs(
    ctx: CollectorContext,
    query: LogQuery,
    span: BlockSpan,
    checkpoint: Checkpoint | None = None,
) -> CollectionResult[RawLog]: ...

def scan_asset_outflows(
    ctx: CollectorContext,
    victim: Address,
    span: BlockSpan,
    standards: tuple[str, ...] = ("erc20", "erc721"),
) -> CollectionResult[AssetTransfer]: ...

def scan_approvals(
    ctx: CollectorContext,
    owner: Address,
    span: BlockSpan,
) -> CollectionResult[ApprovalChange]: ...

def inspect_delegation(
    ctx: CollectorContext,
    address: Address,
    block: BlockRef = "latest",
    bypass_cache: bool = True,
) -> CollectionResult[DelegationState]: ...

def collect_transaction(
    ctx: CollectorContext,
    tx_hash: TxHash,
    include_internal: bool = True,
) -> CollectionResult[TransactionEvidence]: ...

def scan_native_outflows(
    ctx: CollectorContext,
    victim: Address,
    span: BlockSpan,
) -> CollectionResult[NativeTransfer]: ...

def decode_authorizations(
    ctx: CollectorContext,
    transactions: Iterable[RawTransaction],
) -> CollectionResult[AuthorizationEvidence]: ...

def collect_wallet_incident(
    ctx: CollectorContext,
    request: WalletIncidentRequest,
) -> WalletIncidentResult: ...
```

**INFERRED —** Validation errors should raise typed exceptions. Provider outages, caps, deadlines, and unavailable internals should return partial results with exact missing coverage.

---

## 3. Architecture

### Recommendation: option B

**INFERRED —** Choose **FastAPI + Supabase Postgres + one worker + Jinja/HTMX**.

Why:

- SQLite/WAL is technically adequate for one process, but payment webhooks, a worker, browser requests, backups, and eventual multi-host deployment make single-host filesystem ownership a poor boundary.
- Supabase gives Bo managed Postgres, backups, authentication options, and private object storage without operating a database.
- Next.js adds a second application stack without improving collection reliability.
- Vercel is suitable for a marketing shell, but not for long-running collectors or the monitoring worker.
- The Mac mini is acceptable for a closed pilot, not for a paid monitoring claim.

**INFERRED —** Production layout:

```text
Browser / API client
        |
FastAPI web process
        |
Supabase Postgres + private object storage
        |
Postgres-backed durable queue
        |
Single Python worker on an always-on Linux VM/container
        |
Paid Robinhood RPC + Blockscout fallback
```

### Job semantics

**INFERRED —** Use Postgres rather than Celery/Redis initially:

- `investigation_run` is both run record and queue item.
- Claim with `SELECT ... FOR UPDATE SKIP LOCKED`.
- States: `queued`, `leased`, `retry_wait`, `succeeded`, `partial`, `failed_terminal`, `canceled`.
- Lease fields: `leased_by`, `lease_expires_at`, `heartbeat_at`.
- Unique logical key: `(case_id, run_kind, input_hash, collector_version)`.
- On crash, another worker reclaims an expired lease.
- Evidence inserts use immutable natural keys such as `(chain_id, tx_hash, log_index, fact_type)`.
- A rerun may create a new run but should link to the same immutable chain facts.
- Budgets include wall time, RPC subcalls, explorer calls, receipt count, and optional provider cost.
- Each completed block chunk commits evidence and advances its checkpoint in the same database transaction.
- “Succeeded” requires complete mandatory collectors. “Partial” is a first-class terminal result, not failure.

### Monitoring and reorg handling

**VERIFIED —** Robinhood Chain is an Arbitrum Nitro L2; the public RPC is rate-limited and officially not recommended for production. [Robinhood connection documentation](https://docs.robinhood.com/chain/connecting/), [Robinhood node documentation](https://docs.robinhood.com/chain/run-a-full-node/)

**VERIFIED —** Arbitrum advises using `latest`, `safe`, and `finalized` tags rather than a fixed L2 confirmation count. `latest` is sequencer-soft; `safe` has been posted to L1 and reached Ethereum safe status; `finalized` inherits Ethereum finality. [Arbitrum finality guidance](https://docs.arbitrum.io/build-decentralized-apps/troubleshooting-building)

**INFERRED —** Use two cursors:

- `provisional_cursor`: scan to `latest`; urgent alerts say “provisional.”
- `evidence_cursor`: scan only to `finalized`; evidence exports use this cursor.
- Optionally change routine monitoring UI to `safe` for less delay, while preserving `finalized` for reports.

**UNSURE —** Robinhood’s specific RPC support for `safe` and `finalized` tags must be probed before launch. If unsupported, do not invent “N confirmations = final.” Use a second provider and a delayed reconciliation process, while labeling finality unknown.

**INFERRED —** Reorg procedure:

1. Store block number and hash at every checkpoint.
2. Before advancing, re-fetch the saved boundary hash.
3. If it differs, walk backward to a common ancestor.
4. Mark provisional facts from orphaned blocks as superseded; do not delete audit history.
5. Re-run the affected range.
6. Never rewind a finalized checkpoint unless the provider itself reports a severe inconsistency.

### Notification path

**INFERRED —** In the same transaction that records a qualifying assertion, insert a `notification_outbox` row with a deterministic deduplication key. A sender process delivers email first and records attempts. The workspace should always show:

- latest chain head,
- last provisional scan,
- last finalized scan,
- lag in blocks and time,
- current provider health,
- most recent alert delivery,
- any missing coverage.

---

## 4. Data model

**INFERRED —** Use UUID primary keys, UTC timestamps, append-only evidence, JSONB only for irregular payloads, and explicit schema/version columns.

| Status | Table | Core fields | Visibility |
|---|---|---|---|
| INFERRED | `case` | owner, status, case_type, title, retention policy, created/closed timestamps | Private |
| INFERRED | `incident` | case, victim address, chain, suspected type, incident time/range, narrative ciphertext, claimed loss | Private |
| INFERRED | `address` | chain, normalized address, current code classification, first/last observed | Shared chain fact; association with a victim remains private |
| INFERRED | `case_address` | case, address, role, user-supplied flag, private notes | Private |
| INFERRED | `assertion` | case, subject, predicate, object, verdict `confirmed/refuted/unknown`, uncertainty reason, rule/version, temporal scope | Private until exported |
| INFERRED | `evidence_item` | immutable ID, content hash, type, source, observed time, chain/block/hash/tx/log, finality, raw-object reference, run | Private link to shared fact |
| INFERRED | `chain_fact` | chain, fact type, canonical key, canonical JSON/blob hash, block hash, finality | Server-shared, not directly browsable |
| INFERRED | `assertion_evidence` | assertion, evidence, support/contradict relation | Private |
| INFERRED | `label` | address, label, category, provenance URL/provider, source date, valid range, confidence class, reviewer | Global only for public-source labels; otherwise private |
| INFERRED | `relationship` | case, source, destination, relationship type, verdict, uncertainty, method version, temporal scope | Private |
| INFERRED | `relationship_evidence` | relationship, evidence | Private |
| INFERRED | `investigation_run` | case, kind, state, input hash, collector version, budgets, lease, attempts, started/finished, failure classification | Private operational |
| INFERRED | `coverage_checkpoint` | run/subscription, collector, start/end/next block, boundary hash, finality, status, gaps | Private operational; summarized to customer |
| INFERRED | `monitoring_subscription` | case, scope, starts/ends, state, cadence, provisional/finalized cursors, last success, lag | Private |
| INFERRED | `review_decision` | case/assertion/report, reviewer, decision, rationale, evidence snapshot hash | Highly restricted |
| INFERRED | `authorization` | case, granting user, scope, destination, expiry, revocation, terms hash, signature evidence | Highly restricted |
| INFERRED | `report_package` | case, version, redaction profile, manifest hash, artifact URI/hash, generated/reviewed times | Private; share-token access only |
| INFERRED | `submission` | report, destination, authorization, exact payload hash, submitted time, external ID, state | Highly restricted |
| INFERRED | `response` | submission, received time, sender, body/blob, status | Highly restricted |
| INFERRED | `payment` | logical order, provider, method, payment chain, asset, amount, payer, provider ID, tx hash, state, idempotency key | Financial/private |
| INFERRED | `entitlement` | order/case, product, starts/ends, status, source payment | Private |
| INFERRED | `refund` | payment, amount, asset, reason, provider ID/tx, state | Financial/private |
| INFERRED | `audit_event` | actor, action, target type/ID, request ID, IP hash, before/after hashes, timestamp | Security-restricted |
| INFERRED | `notification_outbox` | case, event key, channel, destination reference, payload hash, state, attempts | Private operational |

### Safe cross-case reuse

**INFERRED —** Deduplicate only public chain facts. A global `chain_fact` should contain no case ID, customer ID, email, narrative, victim role, or payment wallet association.

**INFERRED —** `evidence_item` links a case to a shared fact. Derived allegations, labels from the customer, relationships, and conclusions remain case-scoped.

**INFERRED —** Do not expose a global fact lookup endpoint. Users access shared facts only through authorized case links, preventing deduplication from leaking that another victim has reported the same address.

---

## 5. Payments

### Human stablecoin checkout

**INFERRED —** Use a provider adapter rather than direct wallet-address invoices:

```python
class CheckoutProvider(Protocol):
    def create_order(self, order: Order) -> CheckoutSession: ...
    def get_payment(self, provider_id: str) -> ProviderPayment: ...
    def verify_webhook(self, headers: Mapping, body: bytes) -> ProviderEvent: ...
    def refund(self, payment_id: str, amount: Decimal) -> RefundResult: ...
```

**VERIFIED —** Coinbase Payment Acceptance currently documents USDC payments, authorization/capture, refunds, reconciliation events, and signed webhooks. [Coinbase Payment Acceptance](https://docs.cdp.coinbase.com/api-reference/payment-acceptance/overview), [webhook API](https://docs.cdp.coinbase.com/api-reference/payment-acceptance/webhooks/create-webhook)

**UNSURE —** Confirm account eligibility, customer geography, hosted-wallet UI quality, supported mainnet networks, and production approval before selecting it. The API documentation alone is not proof that Bo can offer an acceptable consumer checkout immediately.

**INFERRED —** Never grant entitlement from a browser redirect. Grant it only after a verified webhook or active reconciliation reports settlement/capture.

### x402

**VERIFIED —** Current x402 v2 has Python/FastAPI support, a payment-identifier extension for idempotent retries, and explicit `settlement_pending` semantics. A settlement timeout is indeterminate, not a terminal failure. [x402 seller quickstart](https://docs.x402.org/getting-started/quickstart-for-sellers), [payment identifier](https://docs.x402.org/extensions/payment-identifier), [facilitator settlement behavior](https://github.com/x402-foundation/x402/blob/main/docs/core-concepts/facilitator.md)

**VERIFIED —** The public `x402.org` facilitator is for testnet/development, not the assumed production mainnet service. [x402 facilitators](https://docs.x402.org/dev-tools/facilitators)

**VERIFIED —** Coinbase’s current facilitator supports Base, Base Sepolia, Polygon, Arbitrum One, Monad, World, and Solana—not Robinhood Chain. [CDP Facilitator](https://docs.cdp.coinbase.com/x402/seller/facilitator)

**INFERRED —** Use Base USDC for payments while investigating Robinhood Chain. The investigation chain and payment chain are independent columns and should never be inferred from one another.

**UNSURE —** x402 can express `eip155:4663` at protocol level because it supports dynamically registered EVM networks, but there is no verified production facilitator path for Robinhood Chain. [x402 network support](https://docs.x402.org/core-concepts/network-and-token-support)

### Exactly one job per logical payment

**INFERRED —** Model:

```text
logical_order
  ├── payment_attempt 1
  ├── payment_attempt 2
  └── exactly one entitlement
             └── exactly one initial investigation_run
```

Enforce:

- Unique `(provider, idempotency_key)`.
- Unique provider webhook/event ID.
- Unique settled `(network, tx_hash, transfer_index)`.
- Unique initial job `(case_id, product, input_hash)`.
- Store the x402 payment ID and payment-payload hash.
- A retry must reuse the same payment ID and logical order.

**INFERRED —** Payment state machine:

```text
created
→ awaiting_authorization
→ verified
→ settlement_pending
→ settled
→ entitlement_granted
→ refunded / partially_refunded
```

`settlement_pending` must be reconciled by provider lookup and, where possible, chain receipt plus token-transfer verification. It must never automatically become `failed`.

**INFERRED —** For x402 case creation, the protected handler should only create the durable order/case. The worker must refuse to lease the job until the entitlement is settled. That prevents a long investigation from running merely because a signature verified.

### Agent spending controls

**INFERRED —**

- Advertise an exact `$49` price; do not use variable `upto` billing for case creation.
- Bind the quote to product, request-body hash, amount, asset, network, recipient, expiry, and payment ID.
- Client retry limit: one paid retry using the same payment ID.
- Reject a retried 402 if amount, recipient, network, or asset changed.
- Recommend per-request and daily wallet caps to agent clients.
- Reconciliation must precede creation of a fresh payment after any timeout.

---

## 6. Tests, acceptance, deployment, and cost

### Test status and additions

**VERIFIED —** `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -s -p no:cacheprovider tests` passes all **53 tests**.

**INFERRED —** Add these suites before wallet-drain claims:

- Topic-only ERC-20 and ERC-721 outflow fixtures.
- ERC-1155 single/batch fixtures.
- ERC-20, ERC-721, ERC-1155, EIP-2612, Permit2 allowance, and Permit2 signature-transfer fixtures.
- Type-4 EIP-7702 authorization decoding and delegate replacement/clearing.
- Native direct versus internal-value coverage.
- Provider cap, timeout, malformed batch, missing result, and range-resume tests.
- Deadline injection after every collector checkpoint.
- Reorg fixtures with replaced block hashes and orphaned provisional facts.
- Worker crash and lease-reclaim tests.
- Duplicate checkout webhook and duplicate x402 payment tests.
- `settlement_pending` reconciliation tests.
- Cross-tenant/RLS tests proving one case cannot infer another case.
- Golden report manifest/hash tests.

### MVP acceptance criteria

**INFERRED —**

- Every result names its exact block range, finality, provider, and missing ranges.
- A failed log call can never produce “no activity.”
- Killing the worker after any committed chunk resumes without duplicate evidence.
- The same payment/idempotency key creates one case and one initial job.
- A settlement timeout creates no second charge and no runnable job until reconciled.
- A reorg supersedes provisional evidence and emits at most one correction alert.
- Every report file matches its immutable manifest hash.
- Monitoring UI exposes last successful scan, finalized cursor, lag, and provider health.
- Mandatory collector completeness is at least 99% across synthetic and staged failure runs.
- No public report or third-party submission occurs without an explicit authorization record.

### Deployment and outage behavior

**INFERRED —** Run web and worker as separate always-on processes on a small Linux VM/container platform. Use Supabase Postgres and private object storage. Keep the Mac mini as staging or emergency worker only.

**INFERRED —** When RPC is unavailable for an hour:

- Keep the cursor unchanged.
- Mark monitoring `degraded`; never display “no events.”
- Retry with exponential backoff and jitter.
- Fail over to a second paid RPC.
- Catch up in bounded ranges after recovery.
- Show delayed coverage in the customer workspace.
- Send one service-health notice, not repeated false incident alerts.
- Extend the subscription or credit time if the promised monitoring window lacked coverage.

### Cost estimate

| Status | Item | Estimated cost |
|---|---|---:|
| INFERRED | Deterministic token investigation RPC/explorer traffic | Usually under $0.10–$1 per case on a paid plan; public RPC monetary cost is zero but operationally unacceptable |
| INFERRED | Initial wallet-drain scan | Roughly 30–100+ RPC subcalls plus receipts and explorer pagination; typically cents to low dollars |
| INFERRED | Storage | Usually 1–10 MB/case, effectively under $0.01/month per ordinary case |
| INFERRED | Email | Usually under $0.01 per case unless alert volume is abusive |
| INFERRED | LLM | `$0` if reports remain deterministic; optional narrative generation roughly $0.05–$1 depending on model and context |
| INFERRED | Fixed infrastructure | Approximately $50–$200/month for Postgres, worker/API host, monitoring, email, and a production RPC |
| INFERRED | Human review | Dominant cost: 30–90 minutes, plausibly $25–$225 in labor depending on skill and loaded rate |
| VERIFIED | CDP x402 facilitator | First 1,000 onchain facilitator transactions/month are currently documented as free, then $0.001 each, excluding other costs. [CDP Facilitator](https://docs.cdp.coinbase.com/x402/seller/facilitator) |

**UNSURE —** Provider pricing and Robinhood-specific compute-unit charges must be measured with a real account. Broad `eth_getLogs` queries may be billed more heavily than their small response size suggests.

---

## 7. Argument against building this

**VERIFIED —** Chainabuse already accepts reports, offers free victim guidance, supports private reports, and shares appropriate cases with law enforcement partners. [Chainabuse FAQ](https://chainabuse.com/faq)

**VERIFIED —** Revoke.cash already provides free approval history and revocation across 100+ networks, plus paid approved-transfer detection and continuous monitoring. [Revoke.cash](https://revoke.cash/), [pricing](https://revoke.cash/premium)

**VERIFIED —** ScamSniffer already maintains malicious-domain/address intelligence and performs on-chain drain detection, although its published block-monitoring claim is Ethereum-focused rather than Robinhood-specific. [ScamSniffer](https://www.scamsniffer.io/)

**VERIFIED —** MistTrack offers free wallet tracing and broad AML labels across major chains, though Robinhood Chain is not in its published supported-chain list. [MistTrack documentation](https://docs.misttrack.io/)

**INFERRED —** Explorers already answer the first-order questions for sophisticated users at no charge. The paying customer is therefore not purchasing raw transactions; they are purchasing fast interpretation, explicit coverage, a defensible evidence package, and perhaps competent human review.

**INFERRED —** Many victims will not pay because:

- The money is already gone.
- They expect recovery, which this product cannot promise.
- A $49 automated report may feel like charging for explorer links.
- Stablecoin-only payment is hostile to the least experienced victims.
- The service cannot reliably identify a human owner from on-chain evidence.
- Exchanges and law enforcement may not respond.
- Wallet-drain native-value coverage is incomplete without a trace/indexing provider.
- Monitoring after a completed drain often has little practical value.

**INFERRED —** The product also creates liability: an incorrect label can support harassment, false exchange reports, or public accusations. Automated outreach should not exist.

**INFERRED —** Stop or pivot if, after a bounded pilot:

- Fewer than 5% of qualified free assessments convert.
- Fewer than 10 paid cases/month arrive after 200+ qualified screenings.
- More than 10% of paid cases finish partial because of provider limitations.
- More than 5% of buyers request refunds because the result merely restates explorer data.
- Human support exceeds 15 minutes per $49 case.
- Fewer than 10% of monitored cases generate a useful new finding.
- No exchange, investigator, or victim advocate confirms that the exported package improves handling.
- A paid trace/indexing dependency pushes gross margin below 70%.

---

## 8. Recommended MVP and backlog

### Scope

**INFERRED —** Launch only:

> Robinhood Chain token-rug screening and investigation for one token, with a deterministic evidence package and 30 days of token/dev-wallet monitoring by email.

Include:

- Free triage.
- $49 bounded investigation.
- Existing dev-history, seller acquisition, funding, burn, and relationship evidence.
- Private workspace.
- Immutable JSON plus human-readable HTML/PDF export.
- Provisional alerts and finalized evidence.
- Human stablecoin checkout.

Cut from MVP:

- “My wallet was drained.”
- ERC-721/1155 incident support.
- Native internal-value completeness claims.
- x402.
- Monthly renewals.
- $249 assisted outreach.
- Automatic Chainabuse/IC3/exchange submissions.
- Public accusation pages.
- LLM-generated conclusions.
- Next.js.
- Multiple chains.

**INFERRED —** This scope is less exciting but credible. Selling wallet-drain investigations before solving internal native transfers, Permit2, and historical 7702 authorization discovery would violate the repository’s own evidence-first philosophy.

### Sequenced backlog

1. **Stable collection boundary**

   - Add `sleuth/collectors/types.py`, `context.py`, `rpc.py`, `logs.py`, `facts.py`.
   - Refactor [client.py](/Users/owner/sleuth/sleuth/client.py:52) to return scoped results rather than mutate `errors`.
   - Refactor [config.py](/Users/owner/sleuth/sleuth/config.py:61) to raise typed errors and perform no directory creation.
   - Preserve `flows.py` as the deterministic analysis kernel.

2. **Make investigations replayable**

   - Split collection, analysis, persistence, and presentation in `triage.py`, `trace.py`, and `devhistory.py`.
   - Remove all `Watchlist.save()` calls from analysis.
   - Add collector and rule version to every assertion.
   - Add deadline/resume tests.

3. **Postgres service core**

   - Add `service/app.py`, `service/models.py`, `service/repository.py`, `service/worker.py`, and migrations.
   - Implement case isolation, durable leases, immutable evidence, and audit events.
   - Render workspace pages with Jinja/HTMX.

4. **Real monitoring**

   - Replace [watchlist.py](/Users/owner/sleuth/sleuth/watchlist.py:17) with database subscriptions and checkpoints.
   - Implement provisional and finalized cursors, reorg reconciliation, health/lag display, and notification outbox.
   - Require a paid production RPC before calling monitoring active.

5. **Human checkout**

   - Add `service/payments/base.py` and one provider adapter.
   - Implement signed webhooks, idempotency, reconciliation, entitlements, and refunds.
   - Keep evidence collection entirely independent of payment-chain code.

6. **Evidence package**

   - Add deterministic HTML/PDF and JSON manifests under `service/reports/`.
   - Hash every artifact and provide expiring share links.
   - Keep external submission manual.

7. **Wallet-drain beta**

   - Add `sleuth/collectors/assets.py`, `approvals.py`, `delegation.py`, `native.py`, and `permits.py`.
   - Do not sell it until native/internal and 7702 acceptance tests pass.

8. **x402 only after agent demand**

   - Add a Base-USDC exact-price endpoint with required payment identifiers.
   - Reuse the same order, payment, entitlement, and job tables.
   - Keep Robinhood Chain as the investigation chain and Base as the payment chain.

**VERIFIED —** No repository files were edited.