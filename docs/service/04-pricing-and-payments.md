# 04 — Pricing, unit economics, and payments

## Tiers

| Tier | Price | Includes | Excludes | Status |
|---|---|---|---|---|
| Free token-rug screen | $0 | Headline findings + checked-through block and gap list; the private case URL persists | Deeper passes | IMPLEMENTED (no shareable public summary) |
| Free drain event screen | $0 | Event inventory, live delegation check | Causal verdicts, paid case (checkout refused) | IMPLEMENTED (beta) |
| Pilot case | $29 one-time | Deeper deterministic passes, private workspace, JSON+HTML export, 30 days monitoring | **Any human investigation or support beyond billing** (hard rule) | IMPLEMENTED as a manual USDC-on-Base order the operator confirms by tx hash; enabled only when `SLEUTH_PAY_TO_BASE_USDC` is set |
| Monitoring extension | $9 / month | Continued monitoring with coverage credits for outages | — | DEFERRED |
| Human-reviewed reporting | ~$249 | Reviewed package, drafted exchange/Chainabuse submissions under authorization | — | DEFERRED (counsel + validation) |
| Machine screen (x402) | $0.10 / call in code (UNSURE as a price — set after measuring RPC cost) | Same screen JSON, machine delivery | Cases | IMPLEMENTED behind `SLEUTH_X402=1` with a mock facilitator only |

Pricing is not validated (Codex R2, UNSURE). Bo should test the $29 and $49 descriptions with real users (B-2).
Codex's floor: $29 only works with zero bespoke support and automated credit/refund on collector failure; at
15 minutes of human support per case it is below cost.

## Unit economics (Codex R1 estimates, INFERRED unless tagged)

| Item | Estimate |
|---|---|
| RPC/explorer per token case | under $0.10–$1 on a paid plan (public RPC is free but not production-grade) |
| Initial drain scan | 30–100+ RPC subcalls plus receipts and explorer pages; cents to low dollars |
| Storage | 1–10 MB per case, effectively < $0.01/month |
| Email | < $0.01 per case |
| LLM | $0 (deterministic templates) |
| Fixed infrastructure | ~$50–200/month (host, backups, paid RPC, email, monitoring) |
| Human review | dominant cost: 30–90 min, ~$25–225 per case — why the human tier is deferred |
| CDP x402 facilitator | **VERIFIED** first 1,000 on-chain settlements/month free, then $0.001 each |

Stop/pivot thresholds (Codex R1): < 5% conversion of qualified free screens; < 10 paid cases/month after 200+
screens; > 10% paid cases partial due to provider limits; > 5% refunds for "just explorer links"; support > 15
min per $29 case; gross margin < 70% once a trace/indexing provider is needed.

## Success fees — assessed and rejected

A percentage of "recovered" funds would (1) make a recovery promise the service cannot keep, (2) create a
per-accusation incentive, (3) look exactly like the recovery-scam pattern the FBI warns about, and (4) likely pull
the service into private-investigator and debt-collection licensing. Rejected. Also rejected: a project token,
selling victim data, paid removal of findings, charging for government channel access.

## Human checkout (stablecoin)

IMPLEMENTED (`service/payments/base.py`, `manual.py`): a persisted state machine, not a provider protocol. States are
`created → awaiting → verified → settlement_pending → settled → entitled`, any non-terminal → `failed`,
`settled`/`entitled` → `refunded` (no partial refund). Illegal transitions raise; re-sending the current state is a
no-op (duplicate webhook semantics). The manual flow: the case page shows the receive-only address, the customer sends
USDC on Base, the operator checks the Base explorer **by hand** and enters the tx hash in `/admin/payments`
(Basic auth + per-process CSRF token + lockout). That is an operator attestation, not an on-chain verification: the
code checks only that the hash is well-formed and not already credited to another payment. `entitle()` then flips the
case product, enqueues the deep run and starts the 30-day subscription.

PROPOSED (NOT BUILT): a provider interface (`create_order`, `get_payment`, `verify_webhook`, `refund`) with a Coinbase
Payment Acceptance adapter (**VERIFIED** its docs cover USDC, auth/capture, refunds, signed webhooks; **UNSURE**
account eligibility and consumer UX until Bo applies); an RPC check of the attested Base tx. Rules that already hold
in code: unique `idempotency_key`, unique `(network, tx_hash)` once set, one order → one entitlement → one deep run,
the worker refuses to lease an unsettled deep run, and the payment chain (`base`) and investigation chain (4663) are
separate columns. Rules that depend on the provider adapter: entitlement from a verified webhook rather than an
operator, and a per-transfer index in the uniqueness key.

## x402 stateless screen (Codex R3 objection on R-3, accepted)

- Design: scheme `exact`, network Base (`eip155:8453`), asset USDC, fixed price; **VERIFIED** the public x402.org
  facilitator is testnet-only and CDP's facilitator covers Base/Polygon/Arbitrum One/… but not Robinhood Chain, so
  payment and investigation chains differ by necessity.
- IMPLEMENTED (`service/payments/x402.py`) — **a simulation of the protocol, not a compatible implementation**: the
  402 body lists `accepts` with `network: "base"` (not CAIP-2), the client sends JSON in a `PAYMENT-SIGNATURE` header,
  and the settlement receipt comes back in the JSON body under `payment`, not in a `PAYMENT-RESPONSE` header. The mock
  facilitator accepts any `signature` starting with `ok` (or `crash`, which settles then raises) and does **not** check
  amount, recipient, asset or network. Adapting the wire shape and binding the signed requirements is part of B-4.
- IMPLEMENTED idempotency: `x402_replays` keyed `(payment_id, request_hash, collector_version)` with states
  `computing → computed → settled | failed`. The `computing` row is an atomic claim (a concurrent duplicate is told to
  retry, never recomputes); the response is cached as `computed` **before** settlement; a settle failure leaves the row
  `computed` and the next retry reconciles via `facilitator.lookup(payment_id)` before ever settling again; a compute
  failure deletes the claim and returns 503 with nothing charged. The same payment id with a different request hash or
  collector version is refused (one payment, one screen). A settled payment id writes exactly one ledger row in
  `payments` (`x402:{payment_id}`), and a settlement tx already credited elsewhere is a hard error.
- NOT BUILT: rejection of a retry whose amount/recipient/network/asset differs (the mock cannot see them); real
  facilitator credentials; the feature stays behind `SLEUTH_X402=1`. The design-record condition that x402 should
  follow a side-effect-free collection path is **not met** (the screen calls `triage.run`, which writes the watchlist);
  see 09, entry 2.

## Agent spending controls

Exact price only (no `upto` for case creation); quote bound to product, body hash, amount, asset, network,
recipient, expiry and payment id; one paid retry per payment id; recommend per-request and daily caps to clients.

## Needs Bo's accounts (nothing configured now)

CDP or another mainnet facilitator; a checkout provider; a receiving wallet whose signing material never touches
the service host; a paid RPC; an email provider.
