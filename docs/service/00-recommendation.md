# 00 — Product recommendation

## Recommendation: NARROW, then validate

Build the service core and the free screens; charge only for a self-service case; keep the human-reviewed reporting
tier off until counsel and the 20-victim validation say otherwise. Reject token, success-fee and recovery-claim
monetization outright.

## For building

- **VERIFIED** Sleuth's kernel is real: per-transaction netting (`flows.trades/aggregate`), pool detection that
  separates one-way sinks from venues, three-valued verdicts in triage/trace/dev-history, fail-closed log splitting,
  and a launch-event collector for the Pons V2 launcher contracts. 53 CLI tests pass; the service and collectors add
  27 more (80 total, offline).
- **VERIFIED** The chain has a live, repeating fraud pattern. The Block (2026-09-27, analyst Wazz) documented an
  $18.43M Pons V2 ring: 53 launches, snipe-tax waivers for up to 32 wallets, bundled buys within 1–3 blocks capturing
  82–86% of supply, fee-escrow withdrawals, consolidation and Relay-bridge cash-out. Bitquery counted 467 further
  clusters ($11.6M). The waiver list is readable from verified launcher calldata (`address[] snipeTaxExemptions`), and
  41% of the 601 `launchAndBuy` launches observed in one 8.3-hour window set a non-empty list (sizes 1–31).
- **INFERRED** We found no general tool that covers Robinhood Chain: MistTrack's published chain list omits it,
  ScamSniffer's block monitoring is Ethereum-focused, and explorers answer first-order questions only for users who
  already know what to look for. This is an absence in what we checked, not a verified exclusivity claim.
- **INFERRED** The deliverable a victim or counterparty actually needs — a coverage-stated, three-valued evidence
  package with exact block ranges and stable identifiers — is what Sleuth's philosophy already produces for free in
  the CLI. Wrapping it in persistence, monitoring and export is incremental.

## Against building (kept on the record; see 09-design-record.md)

- **VERIFIED** Chainabuse accepts reports free, supports private reports and shares with law-enforcement partners.
  Revoke.cash provides free approval history and revocation plus paid approved-transfer monitoring. Both already
  exist for the drain case on major chains.
- **INFERRED** Most victims will not pay: the money is gone, they expect recovery (which cannot be promised), a $29–49
  automated report can feel like charging for explorer links, stablecoin-only checkout is hostile to novices, and
  exchanges or law enforcement may never respond.
- **INFERRED** Liability: an incorrect label can support harassment, false exchange reports or public accusations.
  The $249 "assisted outreach" tier reads, from the victim's side, like the recovery-scam pattern the FBI warns about
  (IC3 PSA I-081123).
- **INFERRED** Monitoring after a completed drain often has little practical value; the real use is pre-trade.
- **INFERRED** The skeptic's conclusion: postpone the victim-facing product, keep the free screener, and sell to
  machines (bots, wallets, launchpad tooling), exchanges/compliance and investigators instead.

## Target customers and incident types

| Customer | Incident / need | Door | Pays? |
|---|---|---|---|
| Rug victim on Robinhood Chain | Pons-style launch ring, dev dump, fake burn | "I was rugged" | Rarely (INFERRED); test $29 |
| Drain victim | ERC-20 approval abuse, direct sweep, EIP-7702 delegate drain | "My wallet was drained" | Only large losses (skeptic: $6k+) |
| Bot / wallet / launchpad tooling | Pre-trade screen of a token or launcher | x402 stateless screen | Plausible, unproven |
| Launchpad operator (Pons) | Ring detection across launches | Partnership (deferred) | Bo's call (B-1) |
| Investigators, exchanges, compliance | Evidence package with coverage statement | Export | Deferred tier |

## Comparison

| Option | What it gives | Gap Sleuth fills |
|---|---|---|
| General AI assistant | Narrative over whatever the user pastes; no chain access, no coverage statement, confident guesses | Deterministic numbers, exact block ranges, three-valued verdicts, never "no selling" from a failed call |
| Explorer (Blockscout) | Raw transfers, slow under Cloudflare, no netting, no attribution rules | Per-tx netting, pool vs sink, swap-confirmed sells, launch-ring signatures |
| Chainabuse (free) | Report intake, LE sharing | Evidence to put in the report; not a replacement for Chainabuse |
| Revoke.cash (free/paid) | Approval history and revocation on 100+ networks (UNSURE whether Robinhood Chain is listed) | Robinhood-specific event inventory with coverage; links drain to launch/ring context |
| ScamSniffer, MistTrack | Address intel, tracing on major chains | Robinhood Chain coverage (not in their published lists) |

## What Sleuth adds that we did not find elsewhere (INFERRED)

1. Chain-specific collection within the public RPC's real limits (30,000-block topic windows, 14 req/s, no archive).
2. Three-valued, coverage-stated findings. Target: every result names its block range, finality and missing spans.
   Today (PARTIAL): results carry head, checked-through block, a `complete` flag and a free-text gap list; start block,
   finality and provider are not yet fields on every result.
3. Launch-ring signatures from verified launcher calldata and existing Transfer coverage: waiver list, bundled-buy
   supply share by launch block +3, fee-recipient ≠ sender, proceeds→next-launch edges (versioned heuristics, never
   proof on their own).
4. Attribution discipline that is already code: service ≠ owner, proceeds ≠ funding, sinks ≠ sales, routing hops
   counted once, ownership unknown unless established.
