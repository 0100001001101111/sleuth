"""Plain-language layer for the case page. The verdicts stay exactly as recorded (three-valued, from the DB); this
module only translates predicate keys into questions a victim can read, verdicts into one-line meanings, gap codes
into sentences, and the recorded verdicts into a short summary. Nothing here reads the chain or adds a claim that
is not already in the assertions."""
import time

# predicate -> (question, {verdict word: meaning}); None key = unknown. Keep every sentence true for the rule as built.
QUESTIONS = {
    "transfer_event_from_victim": ("Did tokens leave this wallet in the window?", {
        "YES": "At least one token transfer out of this wallet was recorded in the window; the events are listed below.",
        "no": "No ERC-20 or ERC-721 Transfer event out of this wallet was recorded in the window checked. ERC-1155 transfers and native-coin moves are not read by this screen.",
        None: "The window could not be read completely, so this cannot be answered."}),
    "approval_event_owner_victim": ("Did this wallet sign a token approval in the window?", {
        "YES": "An Approval event from this wallet was recorded. It can be a new allowance or a revocation (set to zero); the amounts are in the events below. An open allowance lets the spender move those tokens later without the key.",
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


def summary(case, result, assertions):
    """Sentences for the 'In plain words' panel, derived only from recorded verdicts and the latest result."""
    try:
        v = {a["predicate"]: a["verdict"] for a in assertions}
        res = result if isinstance(result, dict) else {}
        out = []
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
            if win.get("start") is None:
                return out
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
            out.append("Why the funds moved cannot be told from events: that needs the transaction's calldata or a trace, which this free screen does not read.")
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
