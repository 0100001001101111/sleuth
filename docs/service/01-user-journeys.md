# 01 — User journeys

All journeys share one rule set. Nothing in any journey asks for a seed phrase, a private key, a wallet connection,
a signature, or a transaction from the compromised wallet. Payment is a separate step on a separate chain and never
touches evidence collection. Every screen states the block/time it was checked through and labels stale evidence.

## Journey A — "I was rugged"

| Step | User gives | User sees | Status |
|---|---|---|---|
| 1. Free screen | Token address only (`/screen`) | After the worker runs (20 s budget): triage result (launcher, pool vs sink, swap-confirmed dev sells in the launch window, tracked sellers), decoded `launchAndBuy` calldata (waiver list, fee recipient), the early-buy concentration signal, head and checked-through block, gap list. No paywall. | IMPLEMENTED (PARTIAL: no "did a waiver wallet buy early" cross-check, no burn-claim line, coverage shown as checked-through + gaps rather than "blocks A–B at finality F") |
| 2. Open a case ($29) | Optional email (stored as given; used only as the alert destination) and claimed loss (stored on the case, shown as "reported", never an assertion) | Checkout button on the case page → manual USDC-on-Base order to a receive-only address; the operator confirms the tx hash in `/admin/payments`; the case is then entitled. The private URL (`/c/{id}?k=…`) exists from the free screen onward. Not a wallet connect. | IMPLEMENTED (manual order; no checkout provider; no emailed link re-issue) |
| 3. Deeper passes (worker) | Nothing | `rug_deep` (240 s budget): dev-history across launches attributed to the dev and the seller-group trace with separate verdicts (direct tx link / shared funding / coordinated trading / batch funded / dev connection), each three-valued and "unknown (incomplete)" where budgets expired. | IMPLEMENTED; fee-escrow claims, launch funding and proceeds→next-launch edges NOT BUILT |
| 4. Workspace | — | Runs with their raw results, assertions (subject / predicate / object / verdict / reason / rule version), newest 200 evidence rows, monitoring status, payment state. | PARTIAL: no timeline, fund-flow graph, role labels or "reported vs verified loss" view |
| 5. Monitoring (30 days) | Nothing (addresses default to the screen's tracked sellers + launcher + token, max 12) | Status: provisional and finalized cursors with block hashes, lag, last success, last error, degraded state. Alerts on finalized Transfer logs touching tracked addresses, written to a local JSONL file. | IMPLEMENTED; email/SMS delivery NOT BUILT; no provisional alerts, so no correction alerts either |
| 6. Export | — | Deterministic JSON (canonical, manifest sha256 recomputable with `reports.verify`) and a print-friendly HTML of the same facts. The customer may attach it to a Chainabuse report, an exchange support ticket or an IC3 filing **themselves**; the service sends nothing. | IMPLEMENTED |

## Journey B — "My wallet was drained" (free beta)

| Step | User gives | User sees | Status |
|---|---|---|---|
| 1. Free event screen | Victim address and optional incident time (window: 1 h before to 24 h after; head-anchored when absent) | Event inventory with event-named predicates: `transfer_event_from_victim` (ERC-20/721 by log shape), `approval_event_owner_victim`, `delegation_observed_at_block` (live `0xef0100` check with delegate target, cache bypassed). Causal predicates (`drain_cause`, `key_compromised`, `spender_used_allowance`, `delegate_caused_transfer`) are always **unknown** with the reason. Known limits are listed as gaps. | IMPLEMENTED (beta); native sends (top-level or internal) NOT BUILT — recorded as a gap, never as "none" |
| 2. Guidance (static, no promises) | — | Boundary copy on the site: never asks for keys; no report filing, no freeze requests, no recovery; recovery-scam warning. | PARTIAL: the revoke/7702-reset and "report it yourself" checklist is not on the page yet |
| 3. Case (not sold in MVP) | — | Checkout on a drain case returns 400 ("the drain screen is free during the beta"). Not offered until paginated native/internal history and historical 7702 attribution pass acceptance tests (Codex R2, accepted). | DEFERRED (enforced in code) |

Never a clean negative: when internal native transfers or historical 7702 authorizations are uncovered, the screen
says "unknown (incomplete)" with the exact gap, not "no drain found".

## Journey C — Pre-trade screen (machines, x402)

| Step | Client does | Service does | Status |
|---|---|---|---|
| 1. Request | `POST /v1/screen/token` with `{"token": "0x…"}` | 402 with `accepts`: scheme `exact`, network `"base"` (pilot shape, not the CAIP-2 `eip155:8453` the protocol uses), asset USDC, fixed price $0.10, resource, timeout | IMPLEMENTED behind `SLEUTH_X402=1` |
| 2. Pay | Retries with a `PAYMENT-SIGNATURE` header carrying JSON `{payment_id, signature, …}` | Verify (mock) → atomic `computing` claim → compute → cache as `computed` → settle → `settled`; the receipt is returned in the JSON body under `payment`, not in a `PAYMENT-RESPONSE` header. A retry with the same payment id replays the cached result; the same payment id with a different request or collector version is refused. | IMPLEMENTED with a MOCK facilitator; real wire format and facilitator are part of B-4 |
| 3. Uncertain settlement | Must not re-pay | Row stays `computed`; the next retry asks the facilitator `lookup(payment_id)` and records the settlement if found, else settles once; never resettles blindly | IMPLEMENTED |

The response is the same free-screen JSON; payment buys machine delivery, not different facts. "Stateless" is not yet
true: the screen calls `triage.run`, which still reads and writes the CLI watchlist (`data/watchlist.json`); the request
is replayable and creates no case, but it is not side-effect-free. Payment chain (Base) and investigation chain
(Robinhood Chain, 4663) are independent columns.

## What the user is never asked for, on any journey

Seed phrase, private key, wallet connection, message signature, a transaction from the compromised wallet, exchange
credentials, or payment to "unlock" findings already shown in a free screen.
