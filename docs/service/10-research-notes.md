# 10 — Research notes (2026-10-26)

## Chain and RPC measurements (VERIFIED, live probes against rpc.mainnet.chain.robinhood.com)

| Measurement | Result |
|---|---|
| Chain | Robinhood Chain, id 4663, Arbitrum Orbit (Nitro) L2, ~100 ms blocks, Robinhood-run sequencer; mainnet July 2026 |
| Head at probe time | ≈ 98,811,055 → 98,814,277 |
| Topic-only `eth_getLogs` (no `address`) | Capped at **30,000 blocks** per query: "query spans N blocks … but only 30000 are allowed for this request; narrow the block range, or add an address filter". Address-filtered queries keep the repo's measured ≤10M-block / 10k-log limits. |
| Windowed scan cost | 34 calls over 1M blocks (~1.2 days) returned in 0.1 s cold and 7.5 s once the 14 req/s token bucket engaged. 7 days ≈ 202 calls ≈ 15 s; full history ≈ 3,300 calls ≈ 4 min. |
| OR-list topics | `topics=[[Transfer, Approval], pad(addr)]` works in one call per window; ApprovalForAll shares the owner slot. |
| Finality tags | `latest` 98,814,277; `safe` 98,809,044 (−5,233 blocks ≈ 9.3 min); `finalized` 98,805,255 (−9,022 ≈ 15.9 min). `eth_getLogs` accepts `"safe"`. |
| Blockscout address endpoints | `/addresses/{a}/token-transfers?type=ERC-20` 50 items in 4.5 s; `/transactions` 50 in 3.3 s; `/logs` 0.6 s. Paginated, slow, Cloudflare TLS-fingerprinted (curl_cffi required). Secondary source only. |
| Python / SQLite | Python 3.13.2 on the Mac; bundled SQLite 3.45.3. |

Other repo-documented limits (VERIFIED in README/code): RPC 429 counts batch sub-calls (~15/s), non-archive, no
`debug_trace`, Blockscout `/api/eth-rpc` proxy not useful.

## Launchpad calldata (VERIFIED)

- `PonsV2LaunchAndBuy` at `0xe33e9e479df8802cb0866d5d05258bec4cf62948` is verified on Blockscout. Events:
  `Launched(address indexed token, address indexed curve, address indexed recipient, address launcher, uint256
  quoteSpent, uint256 tokensReceived)`, `Rescued`, ownership events. Write functions: `launchAndBuy`, `rescue`,
  ownership.
- `launchAndBuy(params (name, symbol, logo, description, socials(twitter, telegram, discord, website, farcaster),
  address creatorFeeRecipient, uint16 creatorTaxBps, bool buybackEnabled, bytes32 expectedEconomics, bytes32 salt),
  uint256 launchConfigId, address pairToken, uint256 quoteIn, uint256 minTokensOut, address recipient,
  address[] snipeTaxExemptions)`, selector `0xf85f8e41`. Hand-decoded live tx `0x90758f65…` (token "Phago"):
  exemptions empty, `creatorFeeRecipient` = sender, `creatorTaxBps` 150, `quoteIn` 0.068888 ETH.
- A launch receipt carries 9 logs across the token, the curve (`0x3c1ebae3…`, **not verified**), `0x31ea5b45…`,
  the second launcher `0x7ed598bc…` and the launcher. The waiver list is therefore in launcher calldata, not in a
  curve event.
- Prevalence sample: 660 launches in the last 300,000 blocks (~8.3 h); 601 via `launchAndBuy` (other selectors
  `0xb0da329a` ×32, `0xf955751f` ×15, six rarer); **244 (41%) set a non-empty exemption list**, sizes 1–31; fee
  recipient ≠ sender in 6 of 601. Exemptions are a routine feature, not proof of a rug.

## Market facts (sources)

- The Block, 2026-09-27 (analyst Wazz): $18.43M Pons V2 ring, 53 launches Jul 10–Sep 21 2026; creators waived the 99%
  snipe tax for up to 32 wallets (15–25 used); one bundled tx bought for all within 1–3 blocks capturing 82–86% of
  supply; 25 of 53 used one unverified bundling contract (created Aug 28); creator withdrew from Pons fee escrow;
  consolidation 98 wallets → one address → `0x9d06…` → `0xf268…` batch to 50 addresses including the next launch's
  creator/exempt wallets; cash-out via Relay bridge to Ethereum → DAI. Linking methods: proceeds → next launch
  funding (45), shared batch-funding signer (4), shared collector (4); decoy same-name launches.
- Bitquery: 467 other clusters, $11.6M. Pons: 15–22k token creations/day at peak, ~$1M/day fees; chain ~345k DAU,
  ~$908M TVL (INFERRED from secondary reporting; not re-measured).
- Bridges: canonical bridge 7-day withdrawals; Relay/Across fast bridges — cross-chain claims need bridge-specific
  evidence.

## x402 (VERIFIED from docs unless noted)

- HTTP 402 flow; headers `PAYMENT-SIGNATURE` (client→server) and `PAYMENT-RESPONSE`; requirements fields scheme /
  network / maxAmountRequired / payTo / asset / resource / maxTimeoutSeconds; facilitator `/verify` and `/settle`
  (holds no funds); schemes `exact`, `upto` (EVM only), batch settlement; payment-identifier extension for idempotent
  retries; `settlement_pending` is non-terminal; a timeout after `PAYMENT-SIGNATURE` is unknown, never re-pay.
- Public x402.org facilitator is testnet-only. CDP facilitator: Base, Base Sepolia, Polygon, Arbitrum One, Monad,
  World, Solana — not Robinhood Chain; first 1,000 on-chain settlements/month free then $0.001. Cloudflare docs list
  further mainnets. Python SDK `pip install x402` (FastAPI/Flask extras) — not installed in this phase.
- Consequence: payment chain ≠ investigation chain; `eip155:4663` is expressible but has no verified facilitator.

## Reporting channels

- Chainabuse: free public form; partner API `POST /reports/batch` (address, category, description, chain required;
  optional loss, evidence, consent flags); auth, categories and chains **UNSURE** (docs sub-pages 404).
- Binance's law-enforcement portal (Kodex) is government/LE only — never used; victims go through regular support.
- IC3 PSAs: I-081123 (recovery-scam warning), I-082423 (report with tx details).
- Coinbase Payment Acceptance: USDC, auth/capture, refunds, reconciliation events, signed webhooks (eligibility UNSURE).

## Licensing (INFERRED; counsel required)

- California BSIS: private investigator includes "investigates crimes", "location of lost or stolen property",
  "secures evidence for use in court".
- Nevada NRS 648.012: includes analysis of computerized data not available to the public; AB173 (2015) information-
  security exemption.

## EIP-7702

- Delegation indicator `0xef0100 ‖ address` in account code; authorizations ride in type-0x04 transactions and emit
  no log, so historical authorization discovery needs an index. A delegation change persists even if execution
  reverts. Revoke by delegating to `address(0)`; do not fund the drained address; delegation checkers exist
  (CurveGrid). Reporting (secondary, UNSURE on exact figure): the large majority of observed delegations have been
  malicious ("CrimeEnjoyor" family).
- Sleuth today: `codes()` returns `eoa7702` from the prefix but discards the delegate target and caches EOA results
  for six hours — the collector must bypass that cache and keep the target.

## Existing free/paid services checked

Chainabuse (free reports, private reports, LE partners), Revoke.cash (free approvals + paid monitoring, 100+ chains;
Robinhood Chain listing UNSURE), ScamSniffer (Ethereum-focused block monitoring), MistTrack (Robinhood Chain not in
published chain list).
