# Codex Round 1 brief — Sleuth incident-investigation add-on

You are Codex (GPT), collaborating with Claude on a design for Bo. You LEAD: repository inspection, implementation
feasibility, architecture, reliable collection, payment processing (crypto checkout + x402), testing, deployment,
operational cost. Claude leads product strategy, investigation methodology, adversarial review, UX, evidence standards,
abuse analysis. Neither has authority; challenge Claude's framing where it is wrong. Do NOT edit any file. Read-only.

## The repo (cwd is ~/sleuth; read it yourself, do not trust this summary blindly)
- Python 3.11 read-only CLI, `python3 run.py [--json] {triage,dev-history,trace,burn,tx,contract,enrich,watch}`.
- `sleuth/client.py`: curl_cffi (Cloudflare TLS) Blockscout v2 + JSON-RPC client, per-run time budget (`BudgetExceeded`),
  token-bucket RPC throttle (burst 120, 14/s; RPC counts batch sub-calls), sqlite `data/cache.db` of DURABLE facts only
  (code, receipts, tx bodies, block timestamps, identity; never balances). `Cache.cursor` table exists but is unused.
- `sleuth/onchain.py`: `transfer_logs` (eth_getLogs with 10k-log cap splitting, <=10M-block range), batch variant,
  `swap_evidence`, block<->time, `codes` (EOA/contract/eoa7702), `balances`, `erc20_meta`, `tx_records_full` (receipt logs).
- `sleuth/flows.py`: deterministic per-tx netting -> trades (sell/buy/burn/transfer_in/transfer_out); pool detection.
- `sleuth/triage.py` (20s budget), `trace.py` (70s), `devhistory.py`, `devprofile.py`, `burn.py`, `contract.py`,
  `txdecode.py`, `launch.py` (Pons V2 launchpad events), `labels.py`, `watchlist.py` (data/watchlist.json, per token,
  `last_block` for incremental follow-up — but nothing runs continuously), `report.py` (markdown summaries).
- Every command writes `data/runs/<ts>_<cmd>.json`. 53 offline tests in `tests/` (synthetic fixtures modelled on the
  STOXPREPS case). Config in `sleuth/config.py` + optional `config.json`. Chain 4663 Robinhood Chain:
  RPC https://rpc.mainnet.chain.robinhood.com (non-archive, no debug_trace, 429 at ~15/s), explorer
  https://robinhoodchain.blockscout.com (slow, Cloudflare).
- Everything is TOKEN-centric (rug / dev-selling). There is NO wallet-drain collector: no Approval-event scan,
  no ERC-721, no per-address all-token outbound scan, no EIP-7702 delegation audit, no Permit decoding.

## The product concept (challenge it)
Paid incident-investigation + monitoring service on top of Sleuth. Entry points: "I was rugged", "My wallet was drained".
Free screening -> paid bounded investigation ($49 test price incl. 30d monitoring; $15/mo renewals; $249 human-reviewed
assisted case with authorized outreach) -> private case workspace website -> real monitoring worker -> evidence export ->
human-reviewed reporting to exchanges / Chainabuse / IC3. Payment via stablecoin checkout and x402 for agent clients.
No token. No custody, no signing, no seed phrases. Payment separate from evidence collection.

## What I need from you (Round 1). Write markdown, concrete, cite file paths and line numbers.
1. Repo verdict: what is reusable as-is as a library; what must be refactored into a stable collector interface
   (e.g. the functions use `client.errors` side channels, `SystemExit` in config, `Watchlist` global JSON file, thread
   pools inside `Client`, `_pool` private usage from triage); what stays CLI-only. Where do timeouts erase partial
   findings (be specific: trace.group per-wallet, devhistory, etc.)? Where is cached vs live distinguished?
2. Wallet-drain collection on this chain WITHOUT archive/trace APIs: how would you detect (a) outbound transfers of all
   ERC-20/721 from a victim address (eth_getLogs topic-only filters across all contracts: is that supported here, cost,
   10M-block limit), (b) Approval / ApprovalForAll events granted by the victim, (c) EIP-7702 delegation of the victim
   EOA (eth_getCode 0xef0100 prefix — already in `codes`), (d) native ETH drains (explorer internal txs only — fallback?),
   (e) Permit/Permit2 (no on-chain Approval event — what can be recovered from receipts?), (f) sweeper bots.
   Give a concrete collector module design with function signatures.
3. Architecture: minimal additive service. Propose the concrete stack given Bo is a solo dev who runs Python here
   (Mac mini / launchd today; Vercel + Supabase elsewhere in his projects). Options: (A) FastAPI + SQLite/WAL + single
   worker process + plain HTML/HTMX; (B) FastAPI + Postgres (Supabase) + worker; (C) Next.js portal + Python API.
   Recommend one with reasons; define job queue semantics (durable, idempotent, resume after crash, per-case budget),
   monitoring loop (block-cursor per case, reorg handling on an L2 — what finality to use on Robinhood Chain / Arbitrum
   Orbit?), notification path.
4. Data model: tables for case, incident, assertion, evidence_item (immutable, content-hashed), address, label(with
   provenance), relationship(with verdict/uncertainty), investigation_run, coverage_checkpoint, monitoring_subscription,
   review_decision, authorization, report_package, submission, response, payment, entitlement, refund, audit_event.
   Which fields public/private. How to share immutable chain facts across cases without leaking victim data.
5. Payments: human stablecoin checkout and x402. Research-level knowledge is fine but FLAG what you are unsure of
   (Claude is fetching the current x402 docs in parallel). Design: free assessment endpoint; paid case-create;
   idempotency key -> one durable job per logical payment; unknown settlement != failed; spending caps for agent retry
   loops; reconciliation after outage. State which chains x402 facilitators actually settle on (NOT Robinhood Chain,
   presumably) and what that means (investigation chain != payment chain).
6. Tests + acceptance criteria for the MVP; deployment + ops (where does the worker run, how is "monitoring active"
   proven to the customer, what to do when RPC is down for an hour); cost per case estimate (RPC free? LLM tokens,
   storage, human time).
7. Argue AGAINST building this (one section): what free tools already do (Chainabuse, Revoke.cash, ScamSniffer,
   MistTrack, explorers), why users might not pay, what is unreliable/expensive, what evidence would justify stopping.
8. Your single recommended MVP scope (what to cut from the concept above) and a sequenced backlog with repo paths.

Be blunt. Mark each claim as VERIFIED (you read it in the repo), INFERRED, or UNSURE.
