# 03 — Autonomy, human review, and reporting

## Autonomy model (MVP)

| Layer | Who / what | Rule |
|---|---|---|
| Numbers, flows, verdicts | Deterministic code (`flows`, `labels`, `trace`, `devhistory`, collectors) | Replayable from stored chain facts; every rule has a version |
| Narrative | Templates only | No LLM prose in the MVP (Codex/Claude resolved, entry 5). Evidence-id-constrained LLM prose may be reconsidered once the deterministic package is trusted |
| User claims | IMPLEMENTED as `cases.claimed_loss` (raw text); the design's `assertions … source=user` is NOT BUILT | Displayed as "reported", never merged into verified findings; no narrative field is collected yet |
| Accusations, publication, external reports, restriction requests | Human review + customer authorization record | No code path exists in the MVP to send anything anywhere |

No numeric fraud-probability scores. Heuristics (bundled-buy share, waiver overlap, sweeper pattern) are reported
as named, versioned signals with their inputs, never as a probability.

## Three-valued predicates

Every predicate renders one of **YES**, **no**, **unknown (incomplete)**, with the reason for unknown (budget,
RPC cap, explorer failure, unsupported pattern). A failed call never produces a negative.

Token-rug predicates (existing, VERIFIED in code): dev sold (swap-confirmed), pool vs one-way sink, burn confirmed
(dead ≠ burn), direct tx connection, shared upstream funder, shared service only, bot controller, coordinated
trading, batch funded, dev connection, ownership (always "unknown" unless a rule establishes it).

Launch-ring signals (versioned heuristics; none is proof of a rug on its own):

- IMPLEMENTED `early_buy_concentration_over_threshold` (`sleuth/collectors/ring.py`, rule `coordinated-launch/0.1`):
  YES when ≥ 10 distinct early buyers within launch block … +3 hold ≥ 50 % of supply at the cutoff; `no` only with
  complete Transfer coverage through the cutoff; otherwise unknown. The thresholds are stored as the assertion's object.
  The predicate was renamed from "coordinated_launch" because the rule measures concentration, not who controls the
  buyers.
- IMPLEMENTED `snipe_tax_exemptions_declared` from decoded `launchAndBuy` calldata (count as object).
- NOT BUILT: waiver-list overlap with early buyers, fee recipient ≠ launch sender as a predicate, fee-escrow
  withdrawals, proceeds → next-launch funding edge, decoy same-name launches.

Drain predicates (event-named per Codex R2; the first three IMPLEMENTED in `sleuth/collectors/wallet.py`, the
native-send rows NOT BUILT and therefore unknown with a recorded gap):

| Predicate | Evidence | Can be YES? |
|---|---|---|
| `transfer_event_from_victim` | Transfer log with `from = victim` (ERC-20 three topics / ERC-721 four topics) | yes |
| `approval_event_owner_victim` | Approval / ApprovalForAll log with `owner = victim` | yes |
| `delegation_observed_at_block` | live `eth_getCode` = `0xef0100 ‖ delegate`, block recorded | yes |
| `native_send_top_level` | explorer transaction page, verified against RPC tx + receipt | yes, labelled preview until paginated |
| `native_send_internal` | explorer internal-tx page | unknown until paginated collection exists |
| `drain_cause`, `key_compromised`, `spender_used_allowance`, `delegate_caused_transfer` | needs calldata/trace tying the actor to the movement | unknown in MVP |

Why: a `transferFrom` by an old allowance has `from = victim` but is not "victim sent"; a non-compliant token can
emit Transfer without moving value; a delegation seen after the loss does not prove it caused the loss; an Approval
event proves an allowance-state event, not consent.

## Reporting channels and permissions

| Channel | MVP | Later | Rule |
|---|---|---|---|
| Private workspace + export | yes | — | Capability URL, private by default; export is a file the customer sends |
| Chainabuse | victim files it with the export | Partner API `POST /reports/batch` (address, category, description, chain required; auth/categories/chains UNSURE — docs sub-pages 404) under an authorization record | Never mass-report; one report per authorized case |
| Exchange support | victim uses the exchange's regular support/victim form | Assisted drafting under human review | **Never** LE-only portals (e.g. Binance's Kodex LE portal); never impersonate law enforcement |
| IC3 | victim files (PSA I-082423: include tx hashes, addresses, dates) | — | Service never files for the victim |
| Public pages | `/`, `/methodology`, `/corrections` (static, no search, no accusations) | search page if B-9 says so | No public case pages; ownership stays unknown |
| Freeze / seizure / recovery | never promised; the service does not request freezes or contact exchanges on anyone's behalf | never promised | Recovery-scam warning shown (PSA I-081123) |

Authorization record (schema reserved): case, granting user, scope, destination, expiry, revocation, terms hash,
evidence snapshot hash. Submission record: exact payload hash, time, external id, state; responses stored. Status
tracking shows "drafted / authorized / sent / acknowledged / rejected / no response", never "escalated" without a
record.

## Licensing boundary (INFERRED, needs counsel — see 08)

Claude's lay reading, not legal advice: California BSIS defines private investigation to include investigating
crimes, locating lost or stolen property and securing evidence for use in court; Nevada NRS 648.012 includes analysis
of computerized data not available to the public, with a 2015 information-security exemption (AB173). Automated
self-service output over public chain data is the narrowest footing we could find; whether it is actually outside
those definitions is for counsel (B-3). A human-reviewed "we investigate your case" tier is not offered until counsel
says how.
