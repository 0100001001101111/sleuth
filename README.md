# sleuth — fast, evidence-first on-chain investigation (Robinhood Chain first)

Read-only CLI that answers four questions with transaction evidence, fast enough for a live dump:

| Question | Command | Budget |
|---|---|---|
| Big dump — is the dev selling? | `sleuth triage TOKEN [--window 30m] [--dev A,B] [--track C]` | 20 s |
| Has the dev EVER sold (full life, swap-confirmed)? | `sleuth dev-history TOKEN [--dev A,B]` | 70 s |
| Are these selling wallets connected? | `sleuth trace A,B,C --token TOKEN [--dev D]` | 70 s |
| Did the developer actually burn those tokens? | `sleuth burn TX_HASH [--token TOKEN]` / `sleuth burn TOKEN --window 24h` | — |
| What IS this transaction? | `sleuth tx TX_HASH` (all token flows, nets, swap evidence, labels) | — |
| Is this contract suspicious? | `sleuth contract ADDR` | — |
| What is tracked for a token, and why? | `sleuth watch TOKEN [add ADDR ROLE "reason" --tx HASH]` | — |
| Explorer was slow during a triage run? | `sleuth enrich data/runs/<run>_triage.json` retries the ETH-funding checks | — |

`sleuth` is `bin/sleuth` (or `python3 ~/sleuth/run.py …`). Add `--json` **before** the subcommand for the full structured output.
Every run also writes the full evidence to `data/runs/<timestamp>_<cmd>.json` and prints its path.

No wallets, no signing, no private keys. Nothing is written on chain.

## Install

```
git clone https://github.com/0100001001101111/sleuth.git ~/sleuth && cd ~/sleuth
pip install curl_cffi pytest   # curl_cffi is required: Blockscout sits behind Cloudflare TLS fingerprinting
python3 -m pytest -q tests     # 35 regression tests, offline
```

Python 3.11+. Optional `config.json` in `~/sleuth` (or `$SLEUTH_HOME`) overrides anything in `sleuth/config.py`:
chains (explorer, RPC, timezone, known pools, launchpad events), HTTP timeouts/concurrency, RPC throttle, budgets, known service addresses.

## How it works (and why it is fast)

* **RPC first.** Transfer logs (`eth_getLogs`), receipts, `eth_getCode`, `eth_call` and block headers come from the chain RPC
  (0.1–0.6 s per call). Blockscout is used only for durable identity facts (names, creation tx, internal ETH transfers) and is
  time-capped, best-effort and cached. When Blockscout is degraded (common: 7–10 s per page, 500s) triage still answers in ≈10 s.
* **Receipt-level netting.** Swaps route wallet → router → PoolManager; each tx is netted per address, so a routed sale is one
  sell by the wallet, routers vanish, and a reverted swap (no logs) is no trade. Pool = contract with non-zero net in ≥3 txs
  (plus configured pools and the launchpad bonding-curve market).
* **Launchpad awareness.** Pons V2 launches are found with one `eth_getLogs` on the launcher event (token, market, launcher).
* **Cache** (`data/cache.db`, sqlite): contract code, receipts, ERC-20 identity, address identity, tx bodies. Never balances.
* **Watchlist** (`data/watchlist.json`): per token — launcher, initial recipients, pools, sellers, funders, batch siblings,
  with reasons and tx hashes, plus `last_block` for incremental follow-ups. `trace` and `triage` read and extend it.

## Attribution rules the code enforces

* Internal ETH received inside a wallet's own swap tx is **proceeds**, not funding. Pre-acquisition funding is what matters.
* A shared **service** (bridge solver, BatchTransfer, router, exchange hot wallet, any address with >20k txs) is reported as
  `shared_service_only`, never as shared funding. "Same funding service" ≠ "same owner".
* `direct_tx_connection`, `shared_funding`, `coordinated_trading`, `batch_funded`, `dev_connection` and `ownership` are
  separate fields; ownership stays `unknown` unless proven.
* "No connection found" is reported as exactly that, never as "independent".
* Burns: a `Transfer` to `0x…dEaD` leaves `totalSupply` unchanged; to `0x0` it is supply-reducing. The RPC is not an archive
  node, so the type is inferred from the destination and flagged `inferred`. Burns executed through EIP-7702 delegated
  wallets / internal calls are found from receipt logs, not from `tx.to`.

## Measured (2026-10-22, after the triage speed pass)

| Run | Latency | Requests | Notes |
|---|---|---|---|
| triage GH0ST 30 m window (289 txs), cold cache | 2.8 s | 23 | one plain full-history scan replaces per-seller logs + receipts |
| triage GH0ST 30 m window, warm cache | 2.4 s | 16 | block-timestamp + receipt cache hits |
| triage, Blockscout wedged | ≈ +0–4 s | — | ETH-funding peeks run in background with a 4 s cap; misses surface as a gap and `sleuth enrich` backfills |
| dev-history GH0ST (cached identity) | 0.9 s | 6 | |
| trace 4 batch-funded wallets (2026-10-19) | 95 s | 66 | explorer-bound (internal ETH); use `--budget 180` when Blockscout is slow |

## Heuristics and known limits (read before quoting a verdict)

* **Pool detection**: besides configured pools and the launch-event market, a contract counts as a venue if it both
  received and sent the token (net, ≥3 txs each way) in the window, OR flows one way but independently emits a
  known swap event (v2/v3/v4 `Swap` topic0, `swap_topics` in config) near its activity. Staking, lockers, fee
  splitters and distributors are one-directional sinks with no swap events: tokens moved there are reported as
  "transfer to contract", never as "sold". If the swap-evidence query fails, is sampled over an uncoverable span,
  or a candidate falls past the per-run check cap, the contract is UNRESOLVED and any dev outflow in a tx touching
  it degrades the dev verdict to unknown — it never silently passes as a transfer. Known accepted limit: the
  two-way (≥3/≥3) promotion takes no swap evidence, because bonding-curve venues emit non-standard events; a busy
  two-way staking vault could therefore be misread as a venue (deposits as "sells"). A market identified only by
  the first-mint fallback is a CANDIDATE and must earn venue status like any other contract.
  `scripts/pons_scan.py` does not auto-promote detected contracts to pools.
* **Seller/buyer attribution is edge-scoped**: a wallet is a seller only if its tokens have an edge path to a
  gaining pool inside that tx (routers collapse); an unrelated transfer bundled into the same multicall as someone
  else's swap stays a plain transfer. Burns are attributed by direct dead edges, then via zero-netting burn
  intermediaries, then exact-amount match; an ambiguous remainder is left unattributed rather than guessed.
* **Launcher identity**: a launch-event launcher is proven; a first-mint-signer launcher is a candidate (may be a
  relayer or launch service) and can support "DEV SOLD: YES" but never a firm "never sold" / "none" — those
  degrade to unknown unless `--dev` names the wallet. `dev-history` with an unknown creation block reports
  unknown, never NEVER.
* **Verdicts are three-valued**: `true` = positive on-chain evidence; `false` = checked with complete evidence;
  `null` / "unknown (incomplete)" = a wallet trace failed, >200 token txs were truncated, or the explorer page was
  incomplete. "No link found" is never "independent".
* **Funding vs proceeds**: ETH received inside a tx that also moved the token for that wallet is a swap output or
  refund and is never counted as funding. Shared funding needs the same EOA (not a pool, router, bridge or contract)
  funding >1 wallet before acquisition.
* **Block ↔ time** mapping is ~50 blocks (~5 s) coarse; window edges are approximate and timestamps interpolated.
* **RPC failures** are surfaced as gaps and `coverage.complete=false`; a failed `eth_getLogs` aborts rather than
  reporting "no activity".

## RPC limits learned the hard way (Robinhood Chain, chain id 4663)

* `eth_getLogs`: ≤10 M blocks per query, ≤10,000 logs per query (`launch.enumerate` halves its chunk on that error).
* Rate limit counts **batch sub-calls**: ~150–200 in a burst then HTTP 429, ~15/s sustained. `client.py` runs a token bucket
  (`http.rpc_burst`=120, `http.rpc_rate`=14) and backs off on 429.
* Not an archive node: `eth_call` at past blocks fails. No `debug_trace*`.
* Blockscout's `/api/eth-rpc` proxy returns null results — not a usable fallback.

## Scripts

`scripts/pons_scan.py --hours 48 --limit 150` ranks recent Pons launches by launcher selling / transfers out (RPC only).

## Regression fixture

The STOXPREPS case (token `0xb4d3…ef4d`, launcher `0x1d61…ff3c`, burn tx `0x3020…6cdb`, batch-funded group via
`0xb102…9885`) is encoded in `tests/`: internal EIP-7702 burn, batch-funded siblings with same-block buys, false
shared-service attribution, failed swaps, routed-transfer double counting, pool-candidate pre-filter, launch-and-buy
mint, fee-on-transfer sale+burn split, incomplete evidence → "unknown", proceeds ≠ funding, staking sink ≠ pool.
