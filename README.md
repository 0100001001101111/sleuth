# sleuth — fast, evidence-first on-chain investigation (Robinhood Chain first)

Read-only CLI that answers four questions with transaction evidence, fast enough for a live dump:

| Question | Command | Budget |
|---|---|---|
| Big dump — is the dev selling? | `sleuth triage TOKEN [--window 30m] [--dev A,B] [--track C]` | 20 s |
| Are these selling wallets connected? | `sleuth trace A,B,C --token TOKEN [--dev D]` | 70 s |
| Did the developer actually burn those tokens? | `sleuth burn TX_HASH [--token TOKEN]` / `sleuth burn TOKEN --window 24h` | — |
| Is this contract suspicious? | `sleuth contract ADDR` | — |
| What is tracked for a token, and why? | `sleuth watch TOKEN [add ADDR ROLE "reason" --tx HASH]` | — |

`sleuth` is `bin/sleuth` (or `python3 ~/sleuth/run.py …`). Add `--json` **before** the subcommand for the full structured output.
Every run also writes the full evidence to `data/runs/<timestamp>_<cmd>.json` and prints its path.

No wallets, no signing, no private keys. Nothing is written on chain.

## Install

```
pip install curl_cffi pytest   # curl_cffi is required: Blockscout sits behind Cloudflare TLS fingerprinting
python3 -m pytest -q tests     # 14 regression tests, offline
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

## Measured (2026-10-19, explorer degraded)

| Run | Latency | Requests | Notes |
|---|---|---|---|
| triage STOX 30 m window (103 txs) | 10.1 s | 21 | RPC stages 1.8 s; the rest is an 8 s capped explorer peek |
| triage STOX 3 h window (3,023 txs / 6,720 transfers) | 12.7 s | 23 | RPC stages 4.4 s |
| trace 4 batch-funded wallets | 95 s | 66 | explorer-bound (internal ETH); use `--budget 180` when Blockscout is slow |
| burn tx check | 12 s | 4 | 8 s of it is the explorer method lookup |

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
shared-service attribution, failed swaps, routed-transfer double counting, pool-candidate pre-filter.
