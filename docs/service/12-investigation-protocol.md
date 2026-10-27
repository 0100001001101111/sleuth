# 12 — Drain investigation protocol (the standard, IMPLEMENTED 2026-10-27)

This is the procedure the drain investigation (`sleuth/collectors/drain.py`, rule `drain-investigation/0.1`) follows on
every run, and the procedure any manual follow-up must follow too. It exists because the first automated pass on a real
EIP-7702 drain stopped at the suspects' outbound rows and missed the one exchange-attributable address in the case; a
manual pass found it by following the suspect's tiny test sends to the next address and reading that address back to its
first funding. That manual method is now the collector's standard. Items marked **IMPLEMENTED** run in code with an
offline test in `tests/test_drain.py`; items marked **MANUAL** are still done by hand and are on the backlog (07).

## 1. Identity: match by contract address, never by symbol — IMPLEMENTED

Poisoners spoof the symbols of real assets ("DAI", "DAl.", look-alike glyphs, fake "USDC") to plant rows in a wallet's
history. Every asset test in the collector compares `token.address_hash` against the chain's canonical token table in
`sleuth/config.py`; a symbol match alone proves nothing. Incoming look-alikes are quarantined; a canonical asset SENT to a
look-alike address is the loss.

## 2. Services are not parties — IMPLEMENTED

An address whose explorer tags say *Bundler*, *Entry Point* or *Paymaster* (ERC-4337 relays) carried a transaction for
whoever paid it. The collector marks such a submitter `service: true`, notes it, and profiles nothing beyond its
transaction count. The same holds for bridges, DEX routers and privacy pools in outbound rows: they are labelled by
kind and counted as where money went, never as who took it. "Shared service" is never "linked".

## 3. Rehearsal sends point at the next hop — IMPLEMENTED

Repeated tiny sends to one address are the shape of testing a destination; the run does not claim a later large move
followed. After the loss, a suspect that sends two or more amounts of a swept asset (or of the canonical asset it swapped
the swept asset into in the same transaction, or of the swept native coin), each at most 1 % of what it holds of that
asset, to the same address has rehearsed. A zero-value row is never a send: it has the repetition of a rehearsal and moves
nothing (the second live run counted three such rows as a hop before this rule), so it counts as a destination but never
as a rehearsal, a candidate or a forward. Rehearsal is judged over every destination the suspect paid, not only the rows
the page shows; candidates beyond the cap are recorded as a `capped` gap and turn the negatives unknown. Those rows are flagged
`rehearsal` and the destination becomes a `next_hop_candidate` (EOA or unknown kind only; up to `HOP_CAP` = 3 across
all suspects, rehearsal destinations first). The suspect's rows carry the actual transfers (`ts`, asset, raw, decimals,
tx), so the case page shows the shape, not a summary.

## 4. Profile every next hop back to its FIRST funding — IMPLEMENTED

`hop()` reads up to `HOP_PAGES` = 14 transaction pages and the token-transfer pages of each candidate and records:

- `received`: what it got from the suspect and when (contract-matched swept assets only);
- `first_funding`: the oldest incoming value transfer on EITHER the transaction or the token-transfer pages (a token
  credit can predate the first coin credit), with the sender's explorer label and kind. It is only called "first" when
  both walks reached their oldest page; otherwise it is `earliest_page_only`, the "first funded by an exchange" verdict
  is **unknown** rather than YES, and every negative that depends on it is unknown;
- `services`: every labelled exchange / bridge / privacy-pool counterparty on both page types (failed transactions
  excluded) with direction and count;
- `contact_back`: any transaction the hop sent to the suspect (traffic in both directions is a relationship signal,
  not proof of identity);
- `forwarded`: swept assets it passed on after receiving them, with the recipient's label;
- `pages_complete` / `complete`: whether the walk finished within budget.

An exchange hot wallet or deposit address at the first funding is the one record that exists outside the chain. The
report words it exactly this way: *the exchange holds the account record for that withdrawal; that is a lead for a
law-enforcement request, not a name, and it says who funded this address, not who ran the loot wallet.*

## 5. Delegations: read the delegate's source for hard-coded addresses — IMPLEMENTED

When a live EIP-7702 delegate is verified, `_hardcoded_addresses` pulls `constant NAME = 0x…` from the source and the
report lists them with their names and whether one is the delegate's own deployer. What the code does with a constant is
not established by the parse (an `ENTRY_POINT` constant is not a receiver), so the report never says "forwards to". A
verified name never makes a delegate safe; a live delegate equal to the one a sweep installed is the open door.

## 6. A delegation set without an outflow — IMPLEMENTED (wording), MANUAL (attribution)

A type-4 transaction submitted by a third party appears under the submitter, not the authority, and it may move
nothing. When no transaction read by the run installed the live delegate, the report says so and states that who
installed it is unknown. Finding the installing transaction (a type-4 index or a bundler scan) is on the backlog.

## 7. Verdicts added by this protocol (three-valued)

| Verdict | YES | no | unknown |
|---|---|---|---|
| `recipient_rehearsal_sends` | a recipient made ≥ 2 sends of a swept asset, each ≤ 1 % of its holding, to one address after the loss | recipients' pages and balances read completely, no such pattern | pages or balances incomplete |
| `next_hop_first_funded_by_exchange` | a hop's first funding came from a labelled exchange | every hop walked to its oldest page and none did | any hop incomplete or unprofiled, or any recipient unprofiled (it could have its own hop) |
| `next_hop_touched_exchange` | a hop has a labelled exchange counterparty | every hop complete, none has | any hop incomplete, or any recipient unprofiled |

Budget: the hop walk is the one read that can reach an exchange, so it is not left the scraps. Once a profiled suspect has
produced a next-hop candidate, `HOP_SHARE` (0.35) of the run's budget is reserved for the hop stage: a later suspect pair is
skipped as unknown (a `budget` gap, `suspects_unprofiled`) rather than spend it. The first pair always runs, and no pair is
skipped while no hop exists (a reserve with nothing to spend it on would be waste). The run budget is 480 s
(`INVESTIGATE_BUDGET_S`): the first live run at 330 s left the hop walk about 90 s, and a 14-page walk back to first funding
takes 100-150 s on a slow explorer. On expiry the candidates are listed as `hops_unprofiled`, a `budget` gap is recorded and
the three verdicts stay unknown. Partial hop records are kept.

## 8. What the protocol still does not do (MANUAL, backlog in 07)

- It does not scan a bundler's or a block range's type-4 transactions to find who installed a delegation that moved nothing.
- It does not follow a hop's forwarded funds to a second hop (depth 1 only; `forwarded` rows are listed for a human).
- It does not cluster operators by funding fingerprint (identical seed amounts from a privacy pool, deploys at nonce 0
  minutes apart); that was established by hand on 2026-10-27 and is recorded as a pattern to automate.
- It reads one explorer per chain; a hop with hundreds of pages is reported as partly read rather than walked in full.
