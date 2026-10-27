# Codex Round 2 — challenge and resolution

## Q1 / C1–C4

### C1. Pilot architecture

**INFERRED — Claude is right; I withdraw my Round 1 recommendation to begin with Supabase Postgres.** A single-host pilot should use SQLite WAL behind a repository interface. Demand below ten paid cases per month does not justify a network database.

**VERIFIED —** SQLite WAL permits concurrent readers but only one writer, and all processes must use the same host. Long readers can delay checkpoints. A naïve copy of only the main database is not a reliable WAL backup because committed data may still be in the `-wal` file; use SQLite’s online backup API or a controlled checkpoint. [SQLite WAL documentation](https://www.sqlite.org/wal.html)

**VERIFIED —** The current `Cache` is not the proposed service database: it does not enable WAL, has only `kv` and `cursor` tables, and its Python lock protects one `Cache` instance rather than coordinating web and worker processes. [client.py](sleuth/client.py:20)

**INFERRED —** Webhook/worker contention does not break SQLite if transactions remain short. RPC calls must occur outside transactions; each evidence chunk should then commit `evidence_items`, `coverage_checkpoints`, and the run lease atomically using `BEGIN IMMEDIATE`. A scan must never hold a transaction open while waiting on RPC or Blockscout.

**VERIFIED —** This environment uses SQLite 3.45.3. SQLite documents a rare multi-connection WAL race as fixed in 3.51.3 and specific backports, not 3.45.3. A two-process pilot should therefore upgrade or use a fixed backport before relying on WAL. [SQLite WAL-reset advisory](https://www.sqlite.org/wal.html#walreset)

**INFERRED —** SQLite becomes the wrong boundary when Bo needs a second host, network-mounted storage, high availability, or sustained write contention. Suggested migration triggers are measurable: recurring `SQLITE_BUSY`, p95 write acquisition above 250 ms, or any requirement for two active workers on separate hosts.

**INFERRED —** Minimum pilot tables are `cases`, `investigation_runs`, `coverage_checkpoints`, `evidence_items`, `assertions`, `monitoring_subscriptions`, `notification_outbox`, and `payment_attempts`. Postgres-specific leasing with `SKIP LOCKED` becomes SQLite leasing with `BEGIN IMMEDIATE` plus a conditional lease update.

### C2. Stateless x402 screening

**INFERRED — Claude is substantially right; I withdraw “cut x402 entirely.”** Nothing fundamental prevents an exact-price Base-USDC x402 endpoint for a bounded token screen. It has no case entitlement or long-running job.

**VERIFIED —** Coinbase’s documented proxy flow verifies and settles payment before forwarding a request to the resource server. [Coinbase Bazaar payment flow](https://docs.cdp.coinbase.com/api-reference/v2/rest-api/x402-facilitator/bazaar-mcp-server)

**VERIFIED —** Current Sleuth triage is not stateless: it reads and mutates `Watchlist`, writes pools, launch metadata, tracked roles, and `last_block`, then saves the global JSON file. [triage.py](sleuth/triage.py:115), [watchlist.py](sleuth/watchlist.py:17)

**INFERRED —** Therefore x402 should follow, not precede, separation of collection from persistence. The endpoint should:

- **INFERRED —** Accept a canonical request body and exact price.
- **INFERRED —** Key replay by `(payment_identifier, request_hash, collector_version)`.
- **INFERRED —** Settle before returning the paid result.
- **INFERRED —** Cache the response so an HTTP retry does not recompute or repay.
- **INFERRED —** Return an explicit incomplete result or replayable service error if collection fails after settlement.

**INFERRED —** Claude’s “no reconciliation beyond the facilitator receipt” is too strong. A settlement timeout or lost HTTP response still creates an indeterminate delivery state. Stateless computation removes job leasing; it does not remove payment idempotency, response replay, or reconciliation.

### C3. $29 versus $49

**INFERRED —** Use a $29 founding-pilot price only if the product is genuinely self-service: deterministic report, automated refunds/credits for collector failure, no human review, and average support below five minutes per case.

**INFERRED —** At 15 minutes of human support, $29 is below a sensible floor after payment, infrastructure, and acquisition costs. Raising the price to $49 helps, but “no included investigative support” must still be explicit; otherwise support demand expands with case complexity.

**INFERRED —** The free screen should reveal verified headline findings and evidence links. Payment should buy deeper dev-history/trace passes, persistence, export, and monitoring—not the answer to “was I rugged?”

**UNSURE —** Neither $29 nor $49 is validated. Bo should test both offer descriptions with actual users before treating either as settled pricing.

### C4. Narrow wallet-drain support

**INFERRED —** A narrow collector is shippable as a clearly labeled beta evidence screen, but not yet as a paid causal diagnosis or a clean “no drain found” result.

**INFERRED —** Specific wrong-result shapes exist if its predicates are carelessly named:

- **INFERRED —** An attacker using an old allowance calls `transferFrom(victim, attacker, amount)`. The log has `from=victim`, but reporting “victim sent” or “compromised key sweep” is wrong. A `Transfer` event does not identify the caller.
- **INFERRED —** A malicious or non-compliant token can emit a `Transfer` event naming the victim without changing a meaningful balance. The verified fact is “contract emitted this event,” not automatically “asset ownership moved.”
- **INFERRED —** A live `0xef0100…` delegation observed after the incident cannot prove that delegate caused an earlier drain. The delegation may have changed after the loss.
- **INFERRED —** An `Approval` event with `owner=victim` proves an allowance-state event, not informed consent; it may result from a permit, contract-wallet execution, or delegated execution.

**INFERRED —** Safe predicate names are therefore `transfer_event_from_victim`, `approval_event_owner_victim`, and `delegation_observed_at_block`. `drain_cause`, `key_compromised`, `spender_used_allowance`, and `delegate_caused_transfer` remain `unknown` without calldata/trace evidence tying the actor to the movement.

**INFERRED —** “Top-level native sends from RPC transactions” also needs correction: ordinary JSON-RPC can validate known transaction hashes but cannot enumerate an address’s transaction history. Discovery requires Blockscout, an indexer, or scanning every block.

**INFERRED —** With those constraints, ship the narrow collector in free beta. Do not charge for it until paid cases can cover internal native transfers and historical 7702 attribution—or clearly sell only the narrower event inventory.

## C5–C7 and additional challenge

**INFERRED — C5 resolved.** MVP narratives should use deterministic templates. Evidence-ID-constrained LLM prose can be reconsidered only after the deterministic package is trusted.

**VERIFIED — C6 resolved.** EIP-2612 requires a successful permit to emit `Approval`; Permit2 SignatureTransfer remains a gap.

**VERIFIED — C7 conceded.** Block-span checkpoints are insufficient. `client.rpc()` accumulates completed batch results locally, but if a later `_rpc_once()` raises `BudgetExceeded`, the earlier results are lost to the caller. `tx_records_full()` processes and caches receipts only after the entire `client.rpc()` call returns. [client.py](sleuth/client.py:164), [onchain.py](sleuth/onchain.py:310)

**INFERRED —** Receipt collection needs a deterministic hash-list checkpoint: persist each completed batch, return `completed_hashes`, `missing_hashes`, and `unattempted_hashes`, and resume from the first uncommitted batch. `devhistory` must set a wallet incomplete before collection and mark it complete only after all required stages finish.

**INFERRED —** Claude’s supplied 30,000-block probe invalidates my 9,999,001-block cost formula for address-free filters. I did not independently reproduce the probe, and Robinhood’s current documentation recommends an Alchemy endpoint, so the measured public-endpoint cap and latency must not be assumed to apply to the production provider. [Robinhood connection documentation](https://docs.robinhood.com/chain/connecting/)

**VERIFIED —** Arbitrum documents `latest`, `safe`, and `finalized` semantics, so Claude’s successful Robinhood probe removes the implementation uncertainty around the two-cursor design. [Arbitrum finality guidance](https://docs.arbitrum.io/build-decentralized-apps/troubleshooting-building)

**INFERRED —** Do not implement the snipe-waiver signature until the curve ABI and exact calldata/event field are verified. “At least ten buyers capturing at least 50% by launch block +3” is usable only as a versioned coordination heuristic, with balances computed from complete Transfer coverage through that cutoff; it is not itself proof of a rug.

## Q2. Largest harm

**INFERRED —** The most dangerous failure is a partial or merely correlational finding being rendered as attribution, then forwarded to an exchange or published. That can harm an unrelated wallet owner and expose Bo to legal and reputational damage.

**INFERRED —** The acceptable control is technical, not a disclaimer: v1 is private by default, has no public-page or external-submission capability, labels customer allegations as assertions, never maps shared funding or service use to common ownership, and renders causal attribution `unknown` unless a deterministic rule’s evidence requirements are satisfied. Any later submission requires explicit per-recipient authorization and human review.

## Q3. Revised wallet-drain cost

**INFERRED —** The following uses Claude’s supplied 30,000-block limit and approximately 100 ms blocks. Calls are per topic filter:

| Status | Window | Blocks | RPC calls | Approximate public-RPC time | Tier |
|---|---:|---:|---:|---:|---|
| INFERRED | 24 hours | 864,000 | 29 | Usually within burst; provider-dependent | Free screen |
| INFERRED | 1,000,000 blocks | 1,000,000 | 34 | Supplied probe: fast cold, up to ~7.5 s throttled | Free maximum |
| INFERRED | 7 days | 6,048,000 | 202 | Supplied probe: ~15 s | Paid background case |
| INFERRED | 30 days | 25,920,000 | 864 | Roughly one minute | Paid deep scan |
| INFERRED | Full 98.81M history | 98,810,000 | 3,294 | Supplied probe: ~4 minutes | Explicit paid backfill only |
| INFERRED | Monitoring increment | Usually under 30,000 | 1 per cursor/filter/poll | Sub-second absent throttling | Paid monitoring |

**UNSURE —** Transfer, Approval, and ApprovalForAll may be combinable using an OR-list in `topics[0]`, because each places victim/from/owner in `topics[1]`; this exact filter must be probed. If separate filters are required, multiply the table’s calls by three.

**INFERRED —** A free drain screen should require an incident time and scan no more than a 24-hour or one-million-block window, plus a live delegation check and bounded known-transaction validation. A seven-to-thirty-day scan belongs in a background paid case. Monitoring advances incremental provisional and finalized cursors; it must never rescan full history.

**INFERRED —** Native history has a separate cost model: one Blockscout transaction page supplies at most a bounded preview, while complete direct/internal history requires paginated background collection. A first page must never be labeled complete.

## Q4. Smallest resolution I would sign

**INFERRED — MVP scope:**

- **INFERRED —** One-host FastAPI service, one worker, fixed-version SQLite WAL, repository abstraction, online backups.
- **INFERRED —** Free Robinhood token-rug screen showing complete headline findings and coverage.
- **INFERRED —** $29 founding-pilot case: deeper deterministic analysis, private workspace, export, and 30 days of monitoring; no human investigation included.
- **INFERRED —** Deterministic templates only; no public accusations or automatic submissions.
- **INFERRED —** Narrow wallet-drain event screen as free beta, always incapable of returning a clean negative when internal-native or historical-7702 coverage is missing.
- **INFERRED —** Stateless/replayable x402 token screen on Base USDC after the triage path is made pure; no x402-funded long jobs.

**INFERRED — Phasing:**

1. **INFERRED —** Refactor `Client`, triage, trace, dev-history, and receipt loops into partial-returning collectors; remove analysis-time `Watchlist.save()`.
2. **INFERRED —** Add SQLite service tables, leases, evidence/checkpoint commits, deterministic reports, monitoring, and human checkout.
3. **INFERRED —** Add the x402 screen and measure machine demand.
4. **INFERRED —** Expand drain coverage to paginated native/internal history, historical 7702 indexing, Permit2, NFTs, and cross-chain evidence.
5. **INFERRED —** Move to Postgres only on an operational trigger, not by default.

**INFERRED — Decisions only Bo can make:**

- **INFERRED —** Whether the first payer is the victim, wallet/bot developer, or launchpad partner.
- **INFERRED —** Whether $29 means zero bespoke support and what refund promise accompanies partial coverage.
- **INFERRED —** Whether “wallet-drain beta” is acceptable product language.
- **INFERRED —** Hosting/payment budget, operating jurisdiction, and whether counsel is engaged before any assisted or external-reporting service.

**VERIFIED —** I made no repository changes. **UNSURE —** No output-file path was supplied and the workspace is read-only, so the response above is the deliverable rather than a created file.