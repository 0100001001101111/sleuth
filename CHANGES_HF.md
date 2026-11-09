# Hedge-fund changes to sleuth (kept separate from upstream)

Cloned 2026-10-21 from https://github.com/0100001001101111/sleuth (upstream head 38076dd) into `~/hedge-fund/tools/sleuth`
on Bo's instruction ("DL this for our hedge fund; whenever we run token/coin analysis use this; upgrade parts if you see any").
Runs with the fund's own interpreter (`.venv/bin/python -I run.py …`) and `SLEUTH_HOME=~/hedge-fund/tools/sleuth`, so the
cache, watchlist and run files stay inside the project (`tools/sleuth/data/`), never in `~/sleuth`. Never committed to the
fund repo; this file is the diff record.

## Added: `sleuth launchdump TOKEN [--blocks 3000] [--first-blocks 3] [--from-block N]`
Detects the Pons launch-dump playbook the Chief reconstructed by hand on 2026-10-18 (DMN, UMBRA): a few wallets buy a large
share in the first trading blocks, forward it to one helper contract, and that contract sells it back into the curve through
dozens of fresh caller wallets within seconds. RPC only (Transfer logs, eth_getCode, tx senders). Three-valued verdict:
`true` = first-block buyers took ≥5% of supply and ≥50% of that was sold inside the window (directly or through a hub);
`false` = checked with complete logs; `null` = incomplete. A hub that pulls from the buyer and sells in ONE tx is still
counted "via hub" (the real DMN shape). Tokens launched outside the configured launchpad are located from their first mint.

Validation 2026-10-21 (live, `data/runs/20261021-0738*_launchdump.json`): DMN → yes, 29.0% bought in block 1, 96.6% sold
via hub 0xb069…1214 in 25 txs from 25 distinct callers, first sell 37 blocks after launch (matches the hand finding);
UMBRA → yes, 9.8% bought, 100% sold via hub 0x9ea3…e741 597 blocks later; AESTRODOM → not seen (launcher 1.2%, no sells).
Tests: `tests/test_launchdump.py` (synthetic two-step forward, pull-and-sell-in-one-tx, clean launch, direct sells, unknown,
and the real DMN transfer log from `reports/hf_tasks_20261017/DAEMON_LOOK/` when present).

## Config (`config.json`, project-local)
`known_services` gains the two dump hubs seen so far (labels only; a label is never evidence of ownership) and `budgets.launchdump`.

## Fund-side wiring
`python -m fund coins sleuth ADDR [--window 30m] [--no-launch]` runs triage + launchdump and prints both; `fund coins look … --sleuth`
runs them first and files the run paths as receipts on the coin-look ledger entry (fund/coinlooks.py).

## Note on the HTTP client
Upstream uses `curl_cffi` with a Chrome TLS fingerprint because the Robinhood Blockscout explorer sits behind a browser check.
The fund's standing rule is no header spoofing; the explorer is only used for names/identity, is time-capped and best-effort,
and every verdict rests on the RPC. Bo directed installing this tool as published (2026-10-21).

## Added 2026-10-21 (Bo: "upgrade it to do solana coins as well"): Solana support, public node only
`sleuth --chain solana triage MINT [--window]`, `… launchdump MINT [--blocks 1500]`, `… contract MINT` (mint/freeze authorities,
top holders). Module `sleuth/solana.py`: base58 + program-derived-address math (pump.fun bonding curve from the mint, no
dependency), a paced client for https://api.mainnet-beta.solana.com (JSON-body 429s retried per item; ~2 transaction
fetches per second is what the node sustains), SPL flows from each transaction's pre/post token balances netted per
owner and turned into the same transfer records the EVM code uses, pools = accounts owned by pump.fun / PumpSwap /
Raydium programs + two-way repeat counterparties, launch = oldest signature on the bonding curve (bounded history) or
the mint, launcher = its fee payer. `launchdump` gained a **bundled launch** flag on both chains (≥3 non-launcher
wallets taking ≥25% in the first blocks, sold or not) and uses the supply at launch when tokens were burned since.
Validation (live): HNET → no dump (one wallet 4.2%, sold 80 slots later), triage 2 h complete, authorities revoked;
VEIL → BUNDLED LAUNCH, 14 wallets took 79.9% in the first 3 slots, none sold inside 1,500 slots.
Limits: holders (`getTokenLargestAccounts`) are throttled globally on the public node and usually come back
"unavailable" (a free indexer key would fix it: Bo's call); signatures come from the mint, so plain wallet-to-wallet
transfers after graduation can be missed (reported as a gap); busy coins read only the newest 120 transactions in
`triage` (coverage marked incomplete). trace/burn/watch are not on Solana yet. Tests: `tests/test_solana.py`.

## 2026-11-09: committed to Bo's repo on a fund branch, ported onto the new upstream main
Upstream `main` was rewritten on Bo's laptop (same first four commits under new hashes) and gained seven commits on
2026-10-22 (fail-closed verdicts, 4-5x faster triage, swap-event evidence, dev fee-claim / launch-farm / typo /
bot-controller checks, `dev-history`, `tx`, `enrich`), plus a WIP branch `feat/incident-service` (10-27). The local
history had no common ancestor with it, so the fund work went in two steps:
- `hf/launchdump-solana` (ba590ba): the 10-21 fund changes exactly as validated, on top of the old local main.
- `hf/launchdump-solana-upstream` (a509c39): the same change rebased onto upstream `ac17154`. Conflicts resolved in
  `cli.py` (both command sets kept: dev-history/tx/enrich AND launchdump/solana) and `flows.py` (upstream one-way
  venue detection kept; `norm()` applied to known pools so Solana base58 keys are not lowercased). Suite: 64 pass
  (53 upstream + 11 fund). Live re-check through the fund wiring on DMN reproduced the 10-21 result exactly (29.0%
  bought in the first 3 blocks, 96.6% sold via hub 0xb069…1214, 25 txs from 25 callers) and now also shows the new
  upstream dev-profile lines (1.122 ETH of creator-fee claims, GasliteDrop launch-farm funding).
The working clone now sits on `hf/launchdump-solana-upstream`. Policy is rule H5 in `~/hedge-fund/AGENTS.md`: fund
branches `hf/<topic>`, never `main`, rebase on `origin/main` before a push, merging into `main` is Bo's call.
Fund side: `python -m fund coins look --address ADDR …` now runs sleuth BY DEFAULT (`--no-sleuth` is the recorded
opt-out), and `tools/sleuth/` is git-ignored by the fund repo.
