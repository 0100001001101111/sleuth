"""Plain-language layer for the case page. The verdicts stay exactly as recorded (three-valued, from the DB); this
module only translates predicate keys into questions a victim can read, verdicts into one-line meanings, gap codes
into sentences, and the recorded verdicts into a short summary. Nothing here reads the chain or adds a claim that
is not already in the assertions."""
import time
from datetime import UTC, datetime

# predicate -> (question, {verdict word: meaning}); None key = unknown. Keep every sentence true for the rule as built.
QUESTIONS = {
    "transfer_event_from_victim": ("Did tokens leave this wallet in the window?", {
        "YES": "At least one token transfer out of this wallet was recorded in the window; the events are listed below.",
        "no": "No ERC-20 or ERC-721 Transfer event out of this wallet was recorded in the window checked. ERC-1155 transfers and native-coin moves are not read by this screen.",
        None: "The window could not be read completely, so this cannot be answered."}),
    "approval_event_owner_victim": ("Was a token approval recorded for this wallet in the window?", {
        "YES": "An Approval event naming this wallet as owner was recorded. It can be a new allowance or a revocation (set to zero); the amounts are in the events below. An open allowance lets the spender move those tokens later without the key.",
        "no": "No Approval event from this wallet was recorded in the window checked.",
        None: "The window could not be read completely, so this cannot be answered."}),
    "approval_for_all_event": ("Did this wallet sign a whole-collection NFT approval?", {
        "YES": "An ApprovalForAll event from this wallet was recorded. It can be a grant or a revocation; the flag is in the events below.",
        "no": "No blanket NFT approval from this wallet was recorded in the window checked.",
        None: "The window could not be read completely, so this cannot be answered."}),
    "delegation_observed_at_block": ("Was this wallet delegated to a contract (EIP-7702) at the block checked?", {
        "YES": "At the block checked, the wallet's code pointed at a contract, which lets that contract act as the wallet. It may have changed since, and this says nothing about when it was set or whether it caused the loss.",
        "no": "No delegation was set at the block checked. One could have existed earlier or later; past delegations leave no log.",
        None: "The live delegation check failed, so this cannot be answered."}),
    "drain_cause": ("What caused the loss?", {None: "Events alone cannot say. Finding the cause needs the transaction's calldata or a trace, which this free screen does not read."}),
    "key_compromised": ("Was the private key stolen?", {None: "Cannot be told from events: a transfer out looks the same whether the key, an approval or a delegation moved it."}),
    "spender_used_allowance": ("Did an approved spender take the funds?", {None: "Cannot be told from events: that needs the caller of the transfer, which logs do not record."}),
    "delegate_caused_transfer": ("Did the delegated contract move the funds?", {None: "Cannot be told: the delegation seen now may have been set after the loss."}),
    # drain investigation (sleuth.collectors.drain): mechanism per transaction, destinations, current delegation
    "assets_left_wallet": ("Did any asset leave this wallet?", {
        "YES": "At least one token or native-coin transfer out of this wallet was found in the pages read.",
        "no": "No transfer out of this wallet was found, and the explorer pages for every chain checked were read completely.",
        None: "The wallet's history could not be read completely on every chain, so this cannot be answered."}),
    "eip7702_authorization_by_wallet_submitted_by_other": ("Did someone else submit a transaction carrying this wallet's EIP-7702 authorization, and did assets leave in it?", {
        "YES": "Yes. A type-4 transaction paid for by another address carried an authorization signed by this wallet's key, delegating the wallet to a contract, and assets left the wallet inside that same transaction. This is how most 2025-26 drains work: one signature hands the whole account to code.",
        "no": "No loss transaction carried an authorization from this wallet; every outflow transaction was fetched and read.",
        None: "Not every outflow transaction could be fetched, so this cannot be answered."}),
    "allowance_or_permit_used_by_third_party": ("Did a third party spend an allowance or permit signature from this wallet?", {
        "YES": "Yes. A transaction paid for by another address moved this wallet's tokens through an existing allowance or a signed permit (Permit / Permit2).",
        "no": "No outflow used an allowance or permit; every outflow transaction was fetched and read.",
        None: "Not every outflow transaction could be fetched, so this cannot be answered."}),
    "wallet_key_signed_outflow": ("Did this wallet's own key sign a transaction that sent assets out?", {
        "YES": "Yes. At least one outflow was a transaction signed by this wallet's key. Who held the key at that moment is not something the chain records; a sweeper with the key and the owner look the same.",
        "no": "No outflow was signed by this wallet's key; every outflow transaction was fetched and read.",
        None: "Not every outflow transaction could be fetched, so this cannot be answered."}),
    "third_party_tx_unresolved": ("Did assets leave in a third-party transaction whose mechanism could not be identified?", {
        "YES": "Yes. Assets left in a transaction paid for by someone else that carried no authorization, allowance or permit this collector recognises; the raw transaction is in the details.",
        "no": "Every third-party outflow was explained by an authorization, allowance or permit.",
        None: "Not every outflow transaction could be fetched, so this cannot be answered."}),
    "sweep_followed_inbound_credit": ("Was a sweep made within minutes of money arriving?", {
        "YES": "Yes. At least one loss transaction followed an inbound credit within two minutes. That timing is consistent with an automated watcher on the account; it can also be coincidence, so it is a lead, not a finding.",
        "no": "No loss transaction followed an inbound credit closely.",
        None: "Not every outflow transaction could be fetched, so this cannot be answered."}),
    "activity_on_other_chains": ("Is this wallet active on more than one of the chains checked?", {
        "YES": "Yes. The same address has history on more than one chain; the case chain and the busiest other chain were read. A key that signs on one chain signs on all of them.",
        "no": "Only one of the configured chains knows this address.",
        None: "One or more chains could not be queried, so this cannot be answered."}),
    "wallet_currently_delegated": ("Is this wallet delegated to a contract right now?", {
        "YES": "Yes. When checked, the account's code pointed at a contract (EIP-7702). Revoking token approvals does not undo this: the delegate can still move anything the key could. The delegate's identity and the time of the check are shown in the investigation; a verified wallet-vendor account is normal, an unverified contract set in a loss transaction is the open door.",
        "no": "No delegation was set on the account on any configured chain when checked.",
        None: "The account record could not be read on every chain, so this cannot be answered."}),
    "other_wallets_authorized_to_same_submitter": ("Did the same submitter carry authorizations from other wallets?", {
        "YES": "Yes. The address that submitted the loss transaction also submitted type-4 transactions carrying other accounts' authorizations. Whether anything moved from those accounts was not checked here; who runs the submitter is unknown.",
        "no": "In the transactions read, the submitter carried no other account's authorization.",
        None: "The submitter's history could not be read completely, so this cannot be answered."}),
    "recipient_rehearsal_sends": ("Did a recipient rehearse a move with small test sends?", {
        "YES": "Yes. After the loss a recipient sent repeated tiny amounts of a swept asset (or of what it was swapped into) to one address: the shape of testing a destination. A later large move is not established by this; that address is profiled as a next hop.",
        "no": "No repeated small sends of a swept asset were found in the recipients' pages read.",
        None: "Recipient pages or balances could not be read completely, so this cannot be answered."}),
    "next_hop_first_funded_by_exchange": ("Was a next hop first funded by an exchange?", {
        "YES": "Yes. An address a recipient paid after the loss received its very first funds from a labelled exchange hot wallet. The exchange holds the account record for that withdrawal: a lead for a law-enforcement request, not a name, and it says who funded that address, not who ran the loot wallet.",
        "no": "Every next hop was read back to its first funding and none came from a labelled exchange.",
        None: "A next hop's history could not be walked back to its first funding, so this cannot be answered."}),
    "next_hop_touched_exchange": ("Has a next hop dealt with an exchange?", {
        "YES": "Yes. A next hop has sent to or received from a labelled exchange address in the pages read.",
        "no": "In the pages read, no next hop dealt with a labelled exchange.",
        None: "Next-hop pages could not be read completely, so this cannot be answered."}),
    "recipient_still_holds_swept_asset": ("Does a recipient still hold what was taken?", {
        "YES": "Yes. At least one recipient's balance still contained an asset that left this wallet when read. A balance is a fact about that moment, not a promise it stays.",
        "no": "No recipient held any of this wallet's swept assets when read.",
        None: "Recipient balances could not be read completely, so this cannot be answered."}),
    "recipient_sent_to_exchange_bridge_or_privacy_pool": ("Did a recipient send funds on to an exchange, a bridge or a privacy pool?", {
        "YES": "Yes. After the first loss, at least one recipient sent value to an address the explorer labels as an exchange, bridge or privacy pool. After that point the chain cannot say who received it; an exchange can, with a lawful request.",
        "no": "In the pages read, no recipient sent to a labelled exchange, bridge or privacy pool after the loss. Unlabelled destinations are listed in the investigation.",
        None: "Recipient histories could not be read completely, so this cannot be answered."}),
    # single-transaction explainer
    "eip7702_authorization_in_tx": ("Does this transaction carry an EIP-7702 authorization?", {
        "YES": "Yes. It is a type-4 transaction: the listed account(s) signed a delegation and the sender submitted it. Any transfer out of those accounts in the same transaction was made by the delegate code.",
        "no": "No. It carries no authorization list; whatever moved, moved by a call, an allowance or a permit.",
        None: "The transaction could not be read."}),
    "assets_left_accounts": ("Did assets leave any account in this transaction?", {
        "YES": "Yes. The accounts whose tokens or native coin left are listed with the mechanism for each.",
        "no": "No token or native transfer out of any account was decoded from this transaction.",
        None: "The transfer list was cut short, so this cannot be answered."}),
    "dev_sold_in_window": ("Did the launcher sell in the first 30 minutes?", {
        "YES": "The launcher's wallet sold into a venue within the first 30 minutes after launch.",
        "no": "No sale by the launcher's wallet was recorded in the first 30 minutes (transfer logs complete).",
        None: "The first 30 minutes could not be read completely, so this cannot be answered."}),
    "early_buy_concentration_over_threshold": ("Did the earliest buyers end up holding most of the supply?", {
        "YES": "At the cutoff block, at least ten early buyers together held at least half the supply. This is a threshold signal; it does not show they are one person.",
        "no": "At the cutoff block the early buyers were fewer than ten or together held under half the supply.",
        None: "Early buys could not be read completely, so this cannot be answered."}),
    "snipe_tax_exemptions_declared": ("Did the launch declare snipe-tax exemptions?", {
        "YES": "The launch call listed addresses exempt from the snipe tax; they are shown in the launch panel.",
        "no": "The launch call listed no snipe-tax exemptions.",
        None: "The launch call could not be decoded."}),
    "sale_proceeds_received": ("Were the followed wallets paid for selling the token?", {
        "YES": "At least one followed wallet received ETH or an ERC-20 in the same transaction it gave the token away: a real sale.",
        "no": "None of the followed wallets was paid for giving the token away.",
        None: "Not every followed wallet could be read completely, so this cannot be settled."}),
    "token_parked_in_non_venue_contract": ("Was the token moved into a contract that paid nothing?", {
        "YES": "Token went into a contract that sent nothing back in the same transaction: a transfer, not a sale.",
        "no": "No followed wallet moved the token into a contract that paid nothing.",
        None: "Not every followed wallet could be read completely, so this cannot be settled."}),
    "token_sent_to_bridge": ("Was the token itself sent to a bridge or exchange?", {
        "YES": "A followed wallet sent the token to a configured bridge solver or exchange hot wallet.",
        "no": "No followed wallet sent the token to a known bridge or exchange.",
        None: "Not every followed wallet could be read completely, so this cannot be settled."}),
    "proceeds_left_wallet": ("Did the sale proceeds move on from the sellers' wallets?", {
        "YES": "Money received for sales was sent onward; the hops are in the money trail.",
        "no": "The money received for sales was still in the sellers' wallets when read.",
        None: "No sale was paid, or the pages were incomplete, so there is nothing to follow."}),
    "proceeds_reached_bridge_or_exchange": ("Did the proceeds reach a bridge or exchange?", {
        "YES": "Proceeds reached a configured bridge solver or exchange hot wallet. Beyond that the chain cannot say who received them.",
        "no": "Within the hops followed, proceeds did not reach a known bridge or exchange.",
        None: "Some hops were not followed (depth, fan-out, budget, busy wallet or cut-short page), so this is unknown."}),
    "dev_ever_sold_prior_launches": ("Has this launcher sold on earlier launches?", {
        "YES": "A wallet attributed to the same launcher sold on at least one earlier launch.",
        "no": "No sale was found across the earlier launches attributed to this launcher (history complete).",
        None: "The launcher's history could not be read completely."}),
    "direct_tx_connection": ("Are the tracked wallets directly linked by transactions?", {
        "YES": "Tracked wallets sent value to each other directly.", "no": "No direct transactions between tracked wallets were found.", None: "Could not be settled: coverage incomplete."}),
    "shared_funding": ("Were the tracked wallets funded from the same source?", {
        "YES": "Tracked wallets share an upstream funder that is not a known service.", "no": "No shared upstream funder was found.", None: "Could not be settled: coverage incomplete."}),
    "coordinated_trading": ("Did the tracked wallets trade in step?", {
        "YES": "Tracked wallets traded within the same short windows.", "no": "No trading in step was found.", None: "Could not be settled: coverage incomplete."}),
    "batch_funded": ("Were the tracked wallets funded in one batch?", {
        "YES": "Tracked wallets were funded by one batch-distribution transaction.", "no": "No batch funding was found.", None: "Could not be settled: coverage incomplete."}),
    "dev_connection": ("Are the tracked wallets connected to the launcher?", {
        "YES": "A tracked wallet is linked to the launcher by one of the rules above; who controls it is still unknown.", "no": "No link to the launcher was found with the rules above.", None: "Could not be settled: coverage incomplete."}),
}

GAPS = [  # (prefix of the gap code or text, sentence)
    ("no incident time given", "You did not give a time, so only the most recent hour was checked."),
    ("no_trace", "Native-coin moves made by contracts cannot be seen: this chain has no trace API."),
    ("no_index", "Earlier EIP-7702 delegations leave no log, so only the current one is visible."),
    ("unsupported", "ERC-1155 batch transfers and Permit2 signature transfers are not scanned yet."),
    ("head unavailable", "The chain could not be reached, so nothing was checked."),
    ("rpc_error", "The data provider returned an error; results are partial."),
    ("provider", "The explorer returned data in an unexpected shape; part of it was skipped."),
    ("truncated", "An explorer page was cut short, so part of a wallet's history was not read."),
    ("budget", "Time ran out before everything was read."),
    ("depth", "Some hops were not followed past the depth limit."),
    ("fanout", "A wallet sent to more addresses than the fan-out limit; the extra ones were not followed."),
    ("skipped", "A very busy wallet (bot or hot wallet) was listed but not followed."),
    ("cycle", "A wallet already visited was reached again and not re-followed."),
    ("malformed", "An explorer row was unreadable and was skipped."),
    ("label", "A wallet's label lookup failed; it is shown without one."),
    ("capped", "More chains or addresses were involved than the limit; the case chain and then the busiest were read, the rest are named."),
    ("not_found", "The explorer does not know this transaction on this chain."),
]


def question(predicate):
    """Plain question for a predicate key; unknown keys fall back to the key with spaces."""
    q = QUESTIONS.get(predicate)
    return q[0] if q else str(predicate or "").replace("_", " ")


def meaning(predicate, verdict, reason=None):
    """One line saying what the recorded verdict means; falls back to the recorded reason."""
    q = QUESTIONS.get(predicate)
    key = verdict if verdict in ("YES", "no") else None
    return (q[1].get(key) if q else None) or (reason or "")


def gap_text(gap):
    """Gap (string, or dict with kind/detail) -> (plain sentence, raw text)."""
    if isinstance(gap, dict):
        raw = f"{gap.get('kind', '?')}: {gap.get('detail', '')}".strip(": ")
        code = str(gap.get("kind", ""))
    else:
        raw = str(gap)
        code = raw
    low = (code or "").lower()
    for prefix, sentence in GAPS:
        if low.startswith(prefix):
            return sentence, raw
    return raw, raw


def _amt(raw, decimals):
    """Human amount from a raw integer string; falls back to the raw figure when decimals are unknown."""
    try:
        n, d = int(raw or 0), int(decimals) if decimals is not None else None
    except (TypeError, ValueError):
        return str(raw)
    if d is None:
        return f"{n} raw"
    q = n / (10 ** d)
    return f"{q:,.6f}".rstrip("0").rstrip(".") if q else "0"


def _when(ts):
    """' (2026-10-27 05:12 UTC)' for an epoch observed_at, or '' when unknown: evidence is dated, never 'now'."""
    try:
        return f" ({datetime.fromtimestamp(float(ts), tz=UTC).strftime('%Y-%m-%d %H:%M UTC')})" if ts else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _investigation_words(v, inv):
    """Plain sentences for the drain investigation: how it left, where it went, and the standing risk."""
    out = []
    chains = [c for c in inv.get("chains") or [] if isinstance(c, dict)]
    names = ", ".join(str(c.get("name") or c.get("chain_id")) for c in chains)
    if not chains:
        out.append("The follow-up investigation could not read any chain, so how the funds left is unknown.")
        return out
    out.append(f"The follow-up read this wallet's history on {names}.")
    how = v.get("eip7702_authorization_by_wallet_submitted_by_other")
    if how == "YES":
        out.append("How the funds left: one signature from this wallet's key handed the account to a contract (an EIP-7702 delegation), and another address submitted that transaction and swept the assets in the same block. The key signed that authorization; whether through a wallet prompt that looked routine or because the key itself was copied, the chain does not say.")
    elif v.get("allowance_or_permit_used_by_third_party") == "YES":
        out.append("How the funds left: a third party spent a token allowance or a permit signature from this wallet.")
    elif v.get("wallet_key_signed_outflow") == "YES":
        out.append("How the funds left: transactions signed by this wallet's own key. The chain cannot tell whether the owner or someone holding the key signed them.")
    elif v.get("third_party_tx_unresolved") == "YES":
        out.append("Assets left in a transaction someone else paid for, by a mechanism this collector does not recognise; the raw transaction is in the details.")
    elif how == "no" and v.get("assets_left_wallet") == "no":
        out.append("No transfer out of this wallet was found on the chains read.")
    else:
        out.append("How the funds left could not be settled: not every outflow transaction could be fetched.")
    tied_self = any(r.get("tied_to_loss") for c in chains for r in (c.get("losses") or []) if isinstance(r, dict))
    if tied_self and how == "YES":
        out.append("A transaction signed by the wallet's own key is also tied to the loss (same recipient or same minutes), which points to the key being usable by the sweeper, not only a signed prompt.")
    where = v.get("recipient_sent_to_exchange_bridge_or_privacy_pool")
    if v.get("recipient_still_holds_swept_asset") == "YES":
        out.append("Where it went: at least one recipient still held part of what was taken when read.")
    if where == "YES":
        out.append("At least one recipient sent value on to an exchange, bridge or privacy pool; the names are in the investigation panel. Past that point only the venue can say who received it.")
    elif where == "no":
        out.append("In the pages read, no recipient sent to a labelled exchange, bridge or privacy pool.")
    if v.get("other_wallets_authorized_to_same_submitter") == "YES":
        out.append("The same submitter address carried authorizations from other wallets too; whether their assets moved was not checked.")
    # next hops: the addresses a recipient paid afterwards, read back to their first funding (the exchange lead lives there)
    for hp in inv.get("next_hops") or []:
        if not isinstance(hp, dict):
            continue
        got = [t for t in hp.get("received") or [] if isinstance(t, dict)]
        amts = ", ".join(f"{_amt(t.get('raw'), t.get('decimals'))} {t.get('symbol') or '?'} on {(t.get('ts') or '')[:10]}" for t in got[:4])
        s = f"Recipient {(hp.get('origin') or '')[:12]}… then paid {(hp.get('addr') or '')[:12]}…" + (f" ({amts})" if amts else "")
        s += ", repeated small amounts: the shape of testing a destination (a later large move is not established)." if hp.get("rehearsal") else "."
        cb = hp.get("contact_back")
        if isinstance(cb, dict):
            s += f" {(hp.get('addr') or '')[:12]}… has also sent a transaction back to it ({(cb.get('ts') or '')[:16].replace('T', ' ')} UTC): traffic in both directions."
        ff = hp.get("first_funding") if isinstance(hp.get("first_funding"), dict) else None
        if ff and ff.get("kind") == "exchange":
            s += (f" It was first funded on {(ff.get('ts') or '')[:10]} by {ff.get('label') or (ff.get('from') or '')[:12]}: an exchange withdrawal. "
                  "The exchange holds the account record for that withdrawal; that is a lead for a law-enforcement request, not a name, "
                  "and it says who funded this address, not who ran the loot wallet.")
        elif ff:
            s += (f" It was first funded on {(ff.get('ts') or '')[:10]} by {ff.get('label') or (ff.get('from') or '')[:12] + '…'}"
                  + (" (earliest page read only)." if ff.get("earliest_page_only") else "."))
        else:
            s += " Its first funding was not found in the pages read."
        ex = [sv for sv in hp.get("services") or [] if isinstance(sv, dict) and sv.get("kind") == "exchange"]
        if ex:
            s += " It has dealt with " + ", ".join(sv.get("label") or (sv.get("addr") or "")[:12] for sv in ex[:3]) + "."
        fw = next((f for f in hp.get("forwarded") or [] if isinstance(f, dict)), None)
        if fw:
            s += (f" It passed {_amt(fw.get('raw'), fw.get('decimals'))} {fw.get('symbol') or '?'} on to {(fw.get('to') or '')[:12]}…"
                  + (f" ({fw['label']})" if fw.get("label") else "") + f" on {(fw.get('ts') or '')[:10]}.")
        if not hp.get("complete"):
            s += " This hop was only partly read."
        out.append(s)
    # the delegation check covers every chain the explorer knows the address on, including chains too quiet to be read
    # Every live delegation is named with its chain: a wallet swept on two chains is usually still delegated on both,
    # and the one installed by the sweep is the open door whatever its name.
    names_by = {cid: (p or {}).get("name") or f"chain {cid}" for cid, p in (inv.get("presence") or {}).items()}
    deles = inv.get("delegations") or {str(c.get("chain_id")): c.get("current_delegation") or {} for c in chains}
    live = [(cid, d) for cid, d in deles.items() if isinstance(d, dict) and d.get("delegated")]
    for cid, d in live:
        who = d.get("label") or ("a verified contract" if d.get("verified") else "an unverified contract" if d.get("verified") is False
                                 else "a contract whose verification could not be read")
        s = (f"On {names_by.get(cid, f'chain {cid}')} this wallet was STILL delegated to {who} ({(d.get('delegate') or '')[:12]}…) "
             f"when checked{_when(inv.get('observed_at'))}.")
        if d.get("set_in_loss_tx"):
            s += (f" That delegation was set in loss transaction {d['set_in_loss_tx'][:12]}… submitted by {(d.get('set_by') or '?')[:12]}…: "
                  "whatever its name, it is the open door.")
        elif d.get("verified") and d.get("label"):
            s += " A verified source and a tidy name do not make it safe: drainer kits verify their delegates too. If you did not set it yourself, it is the open door."
        else:
            s += " An unverified delegate is the open door."
        fwd = [f for f in d.get("forwards_to") or [] if isinstance(f, dict) and f.get("addr")]
        if fwd:
            s += (f" Its verified source hard-codes the address {fwd[0]['addr'][:12]}… as {fwd[0].get('name') or '?'}"
                  + (", the address that deployed it" if fwd[0].get("is_creator") else "") + "; what the code does with it was not established here.")
        if d.get("set_by_kind") == "bundler":
            s += " The submitter is a public bundler, a relay service many wallets use: it carried the transaction, it is not the person behind it."
        if not d.get("set_in_loss_tx"):
            s += (" No transaction read by this run installed it: a delegation can be set by a transaction that moves nothing, which does not "
                  "appear in this wallet's own list, so who installed it is unknown.")
        out.append(s)
    if live:
        out.append("Revoking token approvals does nothing about a delegation. Move what is left to a fresh key, and clear the delegation "
                   "from a wallet that shows type-4 authorizations.")
    elif v.get("wallet_currently_delegated") == "no":
        out.append(f"No delegation was set on the account when checked{_when(inv.get('observed_at'))}. What could still let a sweeper in is an open token allowance or the key itself; the drain screen lists approval events in its window, which is not a check of current allowances.")
    earlier = sum(len(c.get("earlier_third_party") or []) for c in chains)
    if earlier:
        out.append(f"{earlier} third-party move(s) happened more than an hour before the time you gave; they are listed with their own total "
                   "and not counted as this loss. If the loss began earlier, run the screen again with an earlier time.")
    excused = sum(len(c.get("third_party_self_authorised") or []) for c in chains)
    if excused:
        out.append(f"{excused} third-party move(s) followed this wallet's own exact-amount approval minutes earlier, the shape of a swap or "
                   "bridge the wallet ordered; they are listed but not counted as loss.")
    if inv.get("quarantine_total"):
        out.append(f"{inv['quarantine_total']} row(s) were set aside as look-alike or impersonating tokens and addresses (address poisoning); they are listed separately and never counted as loss.")
    return out


def summary(case, result, assertions, investigation=None):
    """Sentences for the 'In plain words' panel, derived only from recorded verdicts and the latest result."""
    try:
        v = {a["predicate"]: a["verdict"] for a in assertions}
        res = result if isinstance(result, dict) else {}
        out = []
        if case["kind"] == "tx":
            ex = res.get("explain") if isinstance(res.get("explain"), dict) else {}
            if not ex:
                return ["The transaction has not been read yet."]
            if not ex.get("found"):
                kinds = {g.get("kind") for g in ex.get("gaps") or [] if isinstance(g, dict)}
                if "provider" in kinds:
                    return ["The explorer failed to answer for this hash, so nothing is known yet; the run can be repeated."]
                return ["The explorer does not know this transaction hash on this chain. Check the chain and the hash."]
            accts = [a for a in ex.get("accounts") or [] if isinstance(a, dict)]
            if ex.get("authorizations"):
                out.append(f"This is a type-4 transaction: it carries {len(ex['authorizations'])} EIP-7702 authorization(s). The submitter paid for it; the account(s) that signed the authorization handed their code to a contract.")
            moved = [a for a in accts if a.get("assets_left", bool(a.get("outflows")))]
            for a in moved:
                out.append(f"Assets left {(a.get('account') or '')[:12]}… by: {str(a.get('mechanism') or 'unknown').replace('_', ' ')}. {a.get('evidence') or ''}")
            for a in accts:
                if a not in moved:
                    out.append(f"{(a.get('account') or '')[:12]}… signed an authorization carried by this transaction but none of its assets moved here; its other transactions may tell a different story.")
            if not moved:
                out.append("No account lost assets in this transaction as far as the decoded transfers show.")
            out.append("One transaction shows the mechanism, not the cause: it cannot say who held the key or what the signer was shown. Run the drain screen on the affected wallet for the full picture.")
            return out
        if case["kind"] == "drain":
            win, inp = res.get("window") or {}, res.get("input") or {}
            span = f" (blocks {win.get('start')} to {win.get('end')})" if win.get("start") is not None else ""
            gaps = [g if isinstance(g, str) else str((g or {}).get("kind", "")) for g in (res.get("gaps") or [])]
            if win.get("start") is None:
                out.append("Nothing could be checked: the chain could not be read for this run, so no window was scanned.")
            elif any(g.startswith("time->block lookup failed") for g in gaps):
                out.append(f"The time you gave could not be mapped to blocks, so the most recent 25 hours were checked instead{span}.")
            else:
                if inp.get("incident_ts") is None:
                    out.append(f"No time was given, so only the most recent hour was checked{span}.")
                elif inp.get("hours_after"):
                    out.append(f"You said it happened in the last {int(inp['hours_after'])} hours, so we checked the last {int(inp['hours_after']) + 1} hours{span}.")
                else:
                    out.append(f"We checked from 1 hour before to 24 hours after the time you gave{span}.")
            n = len(res.get("events") or [])
            if win.get("start") is not None:      # the event screen may have failed while the investigation succeeded
                out.append({"YES": f"Tokens did leave this wallet in that window ({n} event(s) listed below).",
                            "no": "No token transfer out of this wallet was recorded in that window."}.get(v.get("transfer_event_from_victim"), "Whether tokens left the wallet could not be fully checked."))
                if v.get("approval_event_owner_victim") == "YES":
                    out.append("An approval event was signed by this wallet in the window; it can be a new allowance or a revocation (see the events below). An open allowance lets a spender move tokens later without the key.")
                elif v.get("approval_event_owner_victim") == "no":
                    out.append("No approval event from this wallet was recorded in that window.")
                target = (res.get("delegation") or {}).get("delegate_target")
                if v.get("delegation_observed_at_block") == "YES":
                    out.append(f"When checked (block {win.get('end')}), the wallet was delegated to contract {target or '(unknown target)'} (EIP-7702). A delegated contract can act as the wallet; this alone does not show it caused the loss, and it may have changed since.")
                elif v.get("delegation_observed_at_block") == "no":
                    out.append(f"No contract delegation was set on the wallet when checked (block {win.get('end')}).")
            inv = (investigation or {}).get("investigation") if isinstance(investigation, dict) else None
            if isinstance(inv, dict):
                out.extend(_investigation_words(v, inv))
            elif win.get("start") is None:
                return out                                  # nothing was read and nothing followed: say so and stop
            else:
                out.append("Why the funds moved cannot be told from events alone. The follow-up investigation runs next: it fetches each outflow transaction, names the mechanism (delegation, allowance, permit or the key itself), profiles the recipients and checks whether the wallet is still delegated.")
            if res and inp.get("incident_ts") is None:
                out.append("If you know roughly when it happened, run the screen again with the time: the right window matters more than anything else.")
        else:
            out.append({"YES": "The launcher sold within the first 30 minutes.", "no": "The launcher did not sell within the first 30 minutes."}.get(v.get("dev_sold_in_window"), "Whether the launcher sold in the first 30 minutes could not be settled."))
            if v.get("early_buy_concentration_over_threshold") == "YES":
                out.append("Early buying was concentrated in a few wallets (a threshold signal, not proof they are one person).")
            if "sale_proceeds_received" in v:
                out.append({"YES": "At least one of the followed wallets was actually paid for selling the token.", "no": "None of the followed wallets was paid for selling the token."}.get(v["sale_proceeds_received"], "Whether the followed wallets were paid could not be settled."))
                if v.get("token_parked_in_non_venue_contract") == "YES":
                    out.append("Token was moved into a contract that paid nothing back: a transfer, not a sale.")
                out.append({"YES": "Proceeds reached a bridge or exchange, after which the chain cannot say who received them.",
                            "no": "Within the hops followed, proceeds did not reach a known bridge or exchange."}.get(v.get("proceeds_reached_bridge_or_exchange"), "Whether proceeds reached a bridge or exchange is unknown: some hops were not followed (see the money trail)."))
            else:
                out.append("The money trail runs next and adds where any sale proceeds went.")
        return out
    except (KeyError, TypeError, ValueError, AttributeError):
        return []


def incident_time(raw_ts, when, local_iso, tz, now=None):
    """Form -> (incident_ts, hours_after). Precedence: raw unix seconds (API/tests), then the dropdown; the exact
    local time is read ONLY when the dropdown says "exact" (a stale picker value must not override "last 6 hours").
    Raises ValueError with a message the form can show."""
    import math
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo
    if (raw_ts or "").strip():
        try:
            ts = float(raw_ts)
        except ValueError:
            raise ValueError("incident_ts must be unix seconds") from None
        if not math.isfinite(ts) or ts <= 0:
            raise ValueError("incident_ts must be unix seconds")
        return ts, None
    if when == "exact":
        if not (local_iso or "").strip():
            raise ValueError("pick the exact date and time, or choose another option")
        try:
            d = datetime.fromisoformat(local_iso.strip())
        except ValueError:
            raise ValueError("the time could not be read; use the date and time picker") from None
        if d.tzinfo is not None:
            raise ValueError("enter the time without a timezone; Pacific time is assumed")
        zone = ZoneInfo(tz)
        d = d.replace(tzinfo=zone)
        # a wall time the clocks skipped (spring forward) does not exist; zoneinfo would silently shift it
        if d.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != d.replace(tzinfo=None):
            raise ValueError("that time does not exist in Pacific time (the clocks skipped it); pick another")
        return d.timestamp(), None
    if when in ("6", "24"):
        h = int(when)
        return (now if now is not None else time.time()) - h * 3600, h
    return None, None
