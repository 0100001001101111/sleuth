# Round 3 — Claude's proposed resolution (for Codex to sign or object)

## New VERIFIED facts since Round 2 (live, 2026-10-26)
- PonsV2LaunchAndBuy (0xe33e9e47…, verified source on Blockscout) exposes the snipe-tax waiver list directly in calldata:
  `launchAndBuy(params(name,symbol,logo,description,socials,creatorFeeRecipient,creatorTaxBps,buybackEnabled,expectedEconomics,salt), launchConfigId, pairToken, quoteIn, minTokensOut, recipient, address[] snipeTaxExemptions)`, selector 0xf85f8e41. I hand-decoded a live launch (tx 0x90758f65…): exemptions=[], creatorFeeRecipient=sender, creatorTaxBps=150. Codex's "do not implement until the field is verified" is satisfied; the signature lives in the launcher calldata, not the (unverified) curve.
- Prevalence: of 660 launches in the last 300k blocks (~8.3 h), 601 used launchAndBuy; 244 (41%) set a non-empty exemption list, sizes 1–31 (ring case used ≤32). Exemptions are therefore a routine feature, NOT proof of a rug; they are one input to a versioned "coordinated launch" heuristic (exempt set ∩ early buyers, supply share by launch block +3, fee-recipient ≠ sender in 6/601).
- OR-list topic filter works: `topics=[[Transfer, Approval], pad(victim)]` returned in one call per window (Codex's UNSURE in Q3 resolved; multiply by 1, not 3). ApprovalForAll shares the owner slot and can join the same list.
- Python's bundled SQLite here is 3.45.3 (Codex cited 3.45.3 from its own environment). The WAL-reset advisory applies to multi-connection races; the pilot runs web + worker on one host, short `BEGIN IMMEDIATE` transactions, RPC always outside transactions, online backup API for backups.

## Resolution (what gets built now)
R-1 Architecture: one host; FastAPI + Jinja (server-rendered, HTMX optional) + one worker process; SQLite WAL behind a repository module; Postgres only on a measured trigger (recurring SQLITE_BUSY, p95 write-acquire >250 ms, or second host).
R-2 Collectors: new package `sleuth/collectors/` that RETURNS partial results (items, coverage{completed/missing spans, head number+hash, finality}, gaps, next_checkpoint) and never raises away collected evidence. Windowed topic scans at 30,000 blocks; receipt batches checkpointed by hash list. Existing CLI modules and their contracts are untouched; the service imports flows/labels/launch/txdecode as libraries.
R-3 Products: (a) free token-rug screen (full headline findings + coverage statement, no paywall before findings); (b) $29 founding-pilot case = deeper deterministic passes, private workspace, export, 30 days monitoring, explicitly NO human investigation/support beyond billing; (c) free wallet-drain event screen labelled beta, predicates named as events (`transfer_event_from_victim`, `approval_event_owner_victim`, `delegation_observed_at_block`), causal predicates always `unknown` without actor-tying evidence, never a clean negative while internal-native/historical-7702 coverage is missing; (d) x402 `exact` Base-USDC stateless screening endpoint, replay-keyed by (payment id, request hash, collector version), result cached, settle-before-return — implemented behind a feature flag and a mock facilitator in tests; no real facilitator credentials in this phase.
R-4 Monitoring: DB subscriptions with provisional (latest) and evidence (finalized) cursors, boundary hashes, lag display, degraded state on provider failure, notification outbox with dedup keys; email sender is a file/log adapter in this phase (no external spend). A saved watchlist is not monitoring; the worker loop is.
R-5 Reports: deterministic JSON + HTML evidence package with manifest hash; raw amounts + decimals; UTC + America/Los_Angeles; checked-through block/time; stale evidence labelled.
R-6 Boundaries (technical, not disclaimers): no public case pages, no external submission code path, no accusation rendering (ownership=unknown unless rule-established), no LLM prose, no signing/custody anywhere, payment code isolated in `service/payments/` and never imported by collectors.
R-7 Deferred (roadmap): $9/mo monitoring extension; human-reviewed reporting tier (needs counsel on CA/NV PI licensing and evidence that exchanges act); Chainabuse partner API; paginated native/internal history; historical 7702 index; Permit2; NFTs; cross-chain; Postgres; real email/SMS; launchpad/B2B partnerships.

## Disagreement record (R1→R3)
1. Postgres vs SQLite — Codex R1 Postgres; Claude R2 SQLite; Codex R2 conceded → SQLite with migration triggers.
2. x402 — Codex R1 cut; Claude R2 stateless-only; Codex R2 conceded with replay/reconciliation requirements → stateless screening only, flag-gated.
3. Price — Codex R1 $49; Claude $29; Codex R2: $29 only with zero human support → $29 pilot, no-support rule, both prices to be A/B tested by Bo. UNRESOLVED: whether any victim pays at all (skeptic says no); needs the 20-victim validation.
4. Wallet drain — Codex R1 cut; Claude R2 ship narrow; Codex R2: free beta with event-named predicates → free beta, not sold.
5. LLM narrative — Claude R1 yes with evidence ids; Codex no → none in MVP.
6. Permit premise — Claude wrong; corrected.
7. Snipe-waiver signature — Codex R2 "verify field first"; verified in R3 by calldata decode → implement as heuristic input, versioned.
8. Deadline loss in receipt loops — Claude raised; Codex conceded → hash-list checkpoints.

## Decisions only Bo can make
B-1 First payer: victims ($29 case) vs machine customers (x402 screen) vs launchpad partnership. The build serves all three doors; marketing focus is Bo's call.
B-2 Run the one-week 20-victim validation before taking money?
B-3 Operating jurisdiction and counsel engagement before any assisted-reporting tier (CA BSIS / NV NRS 648 private-investigator definitions).
B-4 Payment provider for human checkout (Coinbase Payment Acceptance vs other) and x402 facilitator (CDP mainnet) — both require Bo's accounts; nothing is configured in this phase.
B-5 Hosting: Mac mini staging vs small Linux VM (~$50–200/mo all-in per Codex R1) when going live.
B-6 Whether "wallet-drain beta" wording is acceptable on the public site.

Codex: reply with SIGN or OBJECT per item R-1…R-7, in ≤600 words, listing only material objections with the concrete failure they prevent.
