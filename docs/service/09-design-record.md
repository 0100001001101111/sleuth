# 09 — Design record: Codex / Claude disagreements and resolutions

## How the collaboration actually ran

- Codex (OpenAI, via the `codex exec` CLI in read-only sandbox mode, `-C ~/sleuth`, account default model) wrote
  three rounds against the real repository. The Codex MCP server failed to connect this session, so the CLI was the
  channel. Codex had no network access in rounds 2–3 and said so; its chain claims there are INFERRED from
  Claude-supplied probes.
- Claude (this session) wrote three rounds, ran the live RPC/Blockscout probes, and decoded launcher calldata.
- The "harsh customer" skeptic was a **Claude-based agent** role-playing a churning customer. It is not Codex and not
  independent verification; its output is a product critique only.
- Agreement between models is not verification of blockchain facts. Facts tagged VERIFIED in these docs were
  checked against the live chain, the repo, or a primary document by Claude; Codex's VERIFIED tags refer to the repo
  or published docs it read.
- Round files are copied verbatim in `design-rounds/`.

## Disagreement and resolution record

| # | Topic | Codex R1 | Claude | Resolution |
|---|---|---|---|---|
| 1 | Database | Supabase Postgres + VM | SQLite WAL behind a repository (R2) | Codex conceded in R2. SQLite WAL, one host, short `BEGIN IMMEDIATE` transactions, RPC never inside a transaction, online backups; Postgres only on measured triggers (recurring `SQLITE_BUSY`, p95 write-acquire > 250 ms, second host). SQLite 3.45.3 WAL-reset caveat recorded. |
| 2 | x402 | Cut entirely from MVP | Stateless screening only, never case creation (R2) | Codex conceded in R2 **on the condition that x402 follows, not precedes, the separation of collection from persistence**; Codex R3 objected that R-3 omitted crash-state semantics. Accepted and IMPLEMENTED: durable idempotency keyed by payment id + request hash + collector version, response cached before settlement, reconcile uncertain settlements, never resettle blindly, crash boundaries tested, flag-gated with a mock facilitator. **UNRESOLVED**: the precondition is not met — the machine screen calls `triage.run`, which still reads and writes the CLI watchlist (`data/watchlist.json`) during analysis, so the screen is replayable but not side-effect-free. Resolution: the flag stays off until backlog item 1 in 07 (library mode without `Watchlist.save()`) lands. The wire shape is also the pilot's own, not protocol-compatible (04). |
| 3 | Price | $49 bounded investigation | $29 case + $9/month, findings shown before payment (R1/R2) | $29 pilot only with zero human support and automated credits; both prices to be tested. UNRESOLVED: whether any victim pays at all (skeptic says no) → B-2 validation. |
| 4 | Wallet drain | Cut until native-internal and historical-7702 coverage exist | Ship narrow with explicit coverage gaps (R2) | Codex R2: free beta with event-named predicates (`transfer_event_from_victim`, `approval_event_owner_victim`, `delegation_observed_at_block`); causal predicates unknown; never a clean negative; not sold. Claude accepted. |
| 5 | LLM narrative | None | Evidence-id-constrained LLM prose (R1) | Claude conceded: deterministic templates only in the MVP. |
| 6 | Permit premise | EIP-2612 `permit` does emit `Approval` | Claude's brief said it did not | Claude wrong; corrected. Permit2 SignatureTransfer remains a gap → unknown. |
| 7 | Snipe-waiver signature | "Verify the field before implementing" (R2) | Decoded live: `address[] snipeTaxExemptions` in verified launcher calldata (R3) | Implement as a versioned heuristic input, not proof. Codex signed R-2/R-7 in R3 (its own chain checks UNSURE, no network). |
| 8 | Deadline loss in receipt loops | Block-span checkpoints | Receipt loops (`client.rpc`, `tx_records_full`, dev-history receipts) also discard on budget (R2) | Codex conceded: hash-list checkpoints with completed / missing / unattempted; wallets marked incomplete before collection. Build status: block-window checkpoints IMPLEMENTED; receipt hash-list checkpoints NOT BUILT (07, item 2), so the reused dev-history/trace paths still lose receipt work on budget expiry; their results are marked incomplete, never clean. |

Additional Codex R3 objections accepted into the design:

- **R-4 monitoring**: reorg rollback to the common ancestor; transactional invalidation of derived evidence and
  unsent outbox rows; canonical block hash in dedup keys; finalized-only notifications, or an explicit correction
  when a provisional alert already went out.
- **R-5 reports**: manifest hash is canonical JSON excluding its own hash field and includes chain id, request
  parameters, collector/rule versions, RPC identity, covered ranges and boundary block hashes. Build status: hash and
  verify IMPLEMENTED; chain id, RPC, versions, per-run covered ranges and monitoring cursor hashes present; request
  parameters and per-range boundary hashes NOT yet included (02, "Report manifest").

## Round 3 outcome

| Item | Codex | Note |
|---|---|---|
| R-1 architecture | SIGN | |
| R-2 collectors | SIGN | |
| R-3 products incl. x402 | OBJECT → accepted fix | idempotency state machine (above) |
| R-4 monitoring | OBJECT → accepted fix | reorg / dedup (above) |
| R-5 reports | OBJECT → accepted fix | manifest canonicalization (above) |
| R-6 boundaries | SIGN | |
| R-7 deferred items | SIGN | |
| Disagreement record | OBJECT on entry 2 wording → fixed by amending R-3 | |

## The case against building (required round)

Codex R1 §7 and the skeptic both argued against: free alternatives exist (Chainabuse, Revoke.cash, ScamSniffer,
MistTrack), victims rarely pay after the loss, the service cannot promise recovery or identify owners, exchanges may
not respond, and incorrect labels create liability. The resolution keeps these on record as stop/pivot thresholds
(04) and as the reason the human tier is postponed and the victim-facing paid tier is gated on validation (B-2).

## Known unresolved

- Entry 2's precondition (collection separated from persistence before x402) — see the table; flag stays off.
- Whether any victim segment converts (B-2), and whether the validation gate is binding (the code does not enforce it).
- Robinhood's production provider may not share the public endpoint's 30,000-block cap and latency (Codex R2
  caveat; **UNSURE**).
- Chainabuse partner API auth, categories and chains (docs sub-pages returned 404).
- Whether Revoke.cash lists Robinhood Chain (**UNSURE**).
