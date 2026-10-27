# Claude Round 1 — product strategy, methodology, evidence standards, abuse analysis

## 1. Product thesis (strongest version)
Sleuth's real asset is not "an investigation tool"; it is the only evidence-first collector that already understands
Robinhood Chain's launchpad mechanics (Pons V2 launch events, fee escrow, batch-funded sibling wallets, EIP-7702
burns, PoolManager netting) and a watchlist that ACCUMULATES cluster knowledge across tokens. The documented $18.43M
Pons rug ring (The Block, 2026-09-27) was linked by exactly the evidence Sleuth already produces: "proceeds of one
launch pay the key that funds the next", batch distributors funding 24 snipe-tax-exempt wallets, Relay bridge cash-out.
So the compounding product is a **chain-specific cluster registry + case engine**, and the two intake flows are
front doors onto it.

Strongest product shape: one case engine, three doors, one revenue engine.
- Door 1 "I was rugged" (token-centric; Sleuth covers 80% today).
- Door 2 "My wallet was drained" (address-centric; needs a new collector; generic EVM patterns).
- Door 3 "Should I buy / list this?" (pre-trade screen; same collectors, run BEFORE loss). This is the door the
  skeptic review (harsh-customer pass, 2026-10-26) says is the only one with a reliable payer: wallets, bots,
  the launchpad itself, and agents paying per call via x402.

## 2. The case AGAINST (I agree with most of it)
- Victims of $100–800 rugs will not pay $49 for a document; "dev sold" is visible on Blockscout, and losses are
  socially normalized ("that's the game"). The documented ring's $18.4M was spread over 53 launches and thousands
  of holders: median loss is small.
- A paid "investigation + outreach" funnel is the exact FBI recovery-scam pattern (IC3 PSA I-081123). One angry
  post kills the brand. Outcome value is impossible: no freeze, no KYC, no reversal.
- Monitoring churns: the alert fires once or never.
- The human-assisted $249 tier is where PI-licensing risk concentrates (NV NRS 648.012 defines a PI to include
  analysis of computerized data; CA BSIS lists "investigates crimes" and "location of lost or stolen property").
  Software + self-serve reports is a materially safer shape than "we act on your behalf". Needs counsel.
- Evidence that would justify stopping: fewer than 5 of 20 real recent victims willing to pre-pay $49 after seeing
  a free screening summary; zero exchange/Chainabuse responses on 20 free report packages.

## 3. What I therefore recommend (NARROW, not reject)
1. Build the case engine and the "rugged" door first (reuses Sleuth; the ring case is the regression set).
2. Make screening AND the reporting package (IC3-ready, Chainabuse-ready text, exchange support template) FREE.
   This is the anti-recovery-scam stance and the trust asset. Charge for: (a) persistent case workspace with real
   monitoring (worker + coverage ledger), (b) per-call screening API (x402, `exact`), (c) partner plans later.
3. Show findings BEFORE payment: the free screen renders the headline verdicts with evidence links; payment unlocks
   full graph/export/monitoring. Never "pay to find out if you were scammed".
4. Defer the $249 human-reviewed tier until (i) counsel reviews PI licensing for the operating state, and
   (ii) at least one exchange or Chainabuse response exists on a free package. Keep its DATA MODEL (authorization,
   review decisions, submissions, responses) in v1 so nothing is retrofitted.
5. "Drained" door in v1 for three patterns only: ERC-20 approval abuse (transferFrom by a spender the victim
   approved), direct outbound sweep (compromised key), EIP-7702 delegation drain. Permit/Permit2, NFT, native-ETH-only
   and cross-chain are "supported, more evidence needed / unsupported" screening outcomes, not silent partial work.

## 4. Methodology carried over from Sleuth (non-negotiable)
Three-valued verdicts with scope; shared service ≠ shared owner; proceeds ≠ funding; sinks ≠ pools; routed hops
counted once; reverted = no effect; dead ≠ burn; candidate launcher ≠ proven; "no link found" ≠ independent;
incomplete trace never a clean bill; actual sell times, never "simultaneous"; no numeric fraud probability.
New rug signatures to add (deterministic, from the ring case): snipe-tax waiver list in the launch tx + bundled
multi-wallet buy within N blocks + supply share captured; creator fee-escrow withdrawals; proceeds→next-launch
funding edge; decoy duplicate-name launches within 24h; bridge-exit (Relay) as attribution boundary ("funds left
the chain" is a terminal, not "lost").
Drain-door rule: a valid signature does not establish consent; the chain proves the transfer/approval/delegation
and its timing; the victim's account is an ASSERTION field, displayed as such.

## 5. Screening outcomes (free, autonomous)
supported_ready | supported_needs_evidence | specialist_review | ordinary_trading_on_available_evidence |
unsupported (chain/asset/bridge/type) | insufficient_access (explorer down, RPC gap). Every outcome carries
coverage (blocks/time checked), what was NOT checked, and immediate containment guidance for the drain door
(revoke approvals via Revoke.cash, undelegate 7702 to address(0) from a SAFE device, do not fund the drained
address, move remaining assets — with the explicit caveat that a sweeper may front-run and gas must come first).

## 6. Autonomy / human review
Deterministic code: all amounts, netting, classification, coverage, verdict logic (already Sleuth). LLM: narrative
drafting, hypothesis listing, evidence-gap summaries — every sentence must cite an evidence_item id, rendered as
links; an LLM claim without an evidence id is dropped by the renderer. Web/explorer content is data, never
instructions. External actions (publish, submit, accuse) require customer authorization + reviewer approval, per
package, per recipient; bounded factual follow-ups within the approved package are covered; new allegations need
fresh review.

## 7. Pricing hypothesis (to be challenged)
Free: screen + report package. $29 case (full workspace, export, 30 d monitoring) — lower than $49 because the
findings are already visible; the price buys persistence. $9/mo renewal, capped at N addresses + M alerts.
API: $0.10/screen via x402 `exact` on Base USDC (payment chain ≠ investigation chain; x402.org facilitator is
testnet-only, production needs CDP or a self-hosted facilitator — Codex to confirm). Partner/launchpad: custom.
Human-assisted: not sold in v1.

## 8. Abuse analysis
- Weaponized reporting: a user names a competitor's wallet as "the scammer". Mitigation: findings never say
  "owner"; public pages publish only chain-verified relationships; accusations gated by reviewer; victim
  attestation recorded with hash + timestamp; dispute/correction log public.
- Fake victims to get a wallet labelled: same. Plus rate limits and payment ≠ proof of loss.
- Prompt injection via uploaded screenshots/URLs/token names: untrusted content never reaches tool-choosing
  prompts; uploads stored as blobs, never executed; token names rendered escaped.
- Duplicate victims: dedupe by (chain, tx hash) at evidence level; one case may have many "I was affected" rows.
- Sponsor conflict: a launchpad plan can never suppress or soften findings — contract term + public corrections log.
