"""Wallet-drain EVENT collector (beta). Predicates are named after what the chain proves — an event with the victim in
an indexed slot — never after a cause. A Transfer(from=victim) log is emitted by transferFrom via an old allowance,
by a compromised key, by a 7702 delegate and by non-compliant tokens alike; the caller is not in the log. Causal
predicates therefore stay "unknown" unless a later rule ties an actor to the movement. Nothing here can produce a
clean "no drain": internal native transfers and historical 7702 authorizations are not observable from ordinary RPC."""
from ..client import ApiError, BudgetExceeded
from ..onchain import TRANSFER, RpcError, block_at_time, erc20_meta, pad
from .logs import _hash32, addr_topic, data_uint, scan_topics
from .types import Gap, Result, Span

APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"          # Approval(address,address,uint256)
APPROVAL_FOR_ALL = "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31"  # ApprovalForAll(address,address,bool)
DELEGATION_PREFIX = "0xef0100"
META_CAP = 25
BLOCKS_PER_HOUR = 36_000   # ~100 ms blocks (measured); only used for default windows, never for timestamps

UNKNOWN_CAUSES = {   # always unknown from events alone; a future rule may upgrade them with calldata/trace evidence
    "drain_cause": "events do not identify the caller; needs calldata or trace evidence",
    "key_compromised": "a Transfer(from=victim) is also emitted by transferFrom via an allowance or a 7702 delegate",
    "spender_used_allowance": "needs the transferFrom call's msg.sender, not visible in logs",
    "delegate_caused_transfer": "a delegation observed now may post-date the loss",
}
STRUCTURAL_GAPS = [
    Gap("no_trace", "internal (contract-initiated) native transfers: unknown — the RPC has no trace API; explorer-dependent"),
    Gap("no_index", "historical EIP-7702 authorizations: unknown — authorization changes emit no log and there is no index"),
    Gap("unsupported", "ERC-1155 TransferSingle/TransferBatch and Permit2 signature transfers are not scanned in this version"),
]


def classify_event(lg):
    """Raw log record (from scan_topics) -> event-named record, or None if the shape is not one we assert about."""
    t = lg["topics"]
    base = {"tx": lg["tx"], "block": lg["block"], "idx": lg["idx"], "contract": lg["address"], "block_hash": lg.get("block_hash")}
    if t[0] == TRANSFER and len(t) == 3:
        return {**base, "event": "transfer_event_from_victim", "standard": "erc20", "to": addr_topic(t[2]), "value_raw": data_uint(lg["data"])}
    if t[0] == TRANSFER and len(t) == 4:
        return {**base, "event": "transfer_event_from_victim", "standard": "erc721", "to": addr_topic(t[2]), "token_id": data_uint(t[3])}
    if t[0] == APPROVAL and len(t) == 3:
        return {**base, "event": "approval_event_owner_victim", "standard": "erc20", "spender": addr_topic(t[2]), "value_raw": data_uint(lg["data"])}
    if t[0] == APPROVAL and len(t) == 4:
        return {**base, "event": "approval_event_owner_victim", "standard": "erc721", "approved": addr_topic(t[2]), "token_id": data_uint(t[3])}
    if t[0] == APPROVAL_FOR_ALL and len(t) == 3:
        return {**base, "event": "approval_for_all_event_owner_victim", "operator": addr_topic(t[2]), "approved": data_uint(lg["data"]) != 0}
    return None


def asset_events(client, victim, span, checkpoint=None):
    """One OR-topic scan (measured to work on the Robinhood RPC): Transfer/Approval/ApprovalForAll with the victim in
    topics[1] (from / owner). Returns Result of classified events; unclassifiable shapes are counted in a gap."""
    r = scan_topics(client, [[TRANSFER, APPROVAL, APPROVAL_FOR_ALL], pad(victim)], span, checkpoint=checkpoint)
    items, skipped = [], 0
    for lg in r.items:
        ev = classify_event(lg)
        if ev is None:
            skipped += 1
        else:
            items.append(ev)
    if skipped:
        r.gaps.append(Gap("unclassified", f"{skipped} matching logs had an unexpected topic shape and were not asserted about"))
    return Result(items=items, coverage=r.coverage, gaps=r.gaps, next_checkpoint=r.next_checkpoint)


def delegation(client, addr):
    """Live EIP-7702 check — bypasses the 6 h EOA cache on purpose (incident response must see a fresh delegation).
    Returns {delegated: True|False|None, delegate_target, observed_block, observed_block_hash, gaps}."""
    out = {"addr": addr.lower(), "delegated": None, "delegate_target": None, "observed_block": None, "observed_block_hash": None, "gaps": []}
    errs = []
    try:
        blk, code = client.rpc([("eth_getBlockByNumber", ["latest", False]), ("eth_getCode", [addr, "latest"])], errors=errs)
    except (BudgetExceeded, ApiError) as e:
        out["gaps"].append(f"delegation check failed: {type(e).__name__}")
        return out
    if isinstance(blk, dict):
        try:
            out["observed_block"], out["observed_block_hash"] = int(blk["number"], 16), _hash32(blk.get("hash"))
        except (KeyError, ValueError, TypeError):
            out["gaps"].append("malformed block header")
    if not isinstance(code, str):
        out["gaps"].append("eth_getCode failed: " + (errs[0] if errs else "no result"))
        return out
    code = code.lower()
    if not code.startswith("0x") or len(code) % 2 or any(c not in "0123456789abcdef" for c in code[2:]):
        out["gaps"].append("eth_getCode returned a malformed value; recorded as unknown")
        return out
    if code.startswith(DELEGATION_PREFIX) and len(code) >= len(DELEGATION_PREFIX) + 40:
        out["delegated"] = True
        out["delegate_target"] = "0x" + code[len(DELEGATION_PREFIX):len(DELEGATION_PREFIX) + 40]
    elif out["observed_block"] is None:  # a 'no' is an observation AT a block; without one it is unknown
        out["gaps"].append("delegation observed without a block anchor; recorded as unknown")
    else:
        out["delegated"] = False
        out["code_kind"] = "eoa" if code == "0x" else "contract"
    return out


def _window_for(client, head, incident_ts, hours_before, hours_after):
    """Incident-anchored block window; falls back to a head-relative window (and says so) if time->block fails."""
    hb, ht = head["block"], head["block_time"]
    if incident_ts is None:
        return Span(max(0, hb - hours_before * BLOCKS_PER_HOUR), hb), ["no incident time given: scanned the most recent window only"]
    try:
        lo = block_at_time(client, incident_ts - hours_before * 3600, hb, ht)
        hi = block_at_time(client, min(incident_ts + hours_after * 3600, ht), hb, ht)
        return Span(max(0, int(lo)), min(hb, max(int(hi), int(lo)))), []
    except (BudgetExceeded, ApiError, RpcError, ValueError, TypeError) as e:   # a lookup failure must widen, never silently narrow
        return Span(max(0, hb - (hours_before + hours_after) * BLOCKS_PER_HOUR), hb), [f"time->block lookup failed ({type(e).__name__}); used a head-relative window"]


def drain_screen(client, victim, incident_ts=None, hours_before=1, hours_after=24, span=None):
    """Free screen: event inventory + live delegation + event-named predicates. Three-valued: True / False (only when
    coverage is complete) / None=unknown. Structural gaps are always attached so the result can never read as 'clean'."""
    victim = victim.lower()
    out = {"stage": "drain-screen", "chain_id": client.chain_id, "victim": victim, "predicates": {}, "causal": {},
           "events": [], "tokens": {}, "gaps": [], "coverage": None, "delegation": None}
    try:
        head = client.head()
    except (ApiError, BudgetExceeded) as e:
        out["gaps"].append(f"head unavailable: {e}")
        out["predicates"] = {k: None for k in ("transfer_event_from_victim", "approval_event_owner_victim", "delegation_observed_at_block")}
        out["causal"] = {k: {"verdict": None, "reason": v} for k, v in UNKNOWN_CAUSES.items()}
        return out
    out["head"] = {"block": head["block"], "block_time": head["block_time"]}
    if span is None:
        span, notes = _window_for(client, head, incident_ts, hours_before, hours_after)
        out["gaps"].extend(notes)
    out["window"] = {"start": span.start, "end": span.end}
    out["delegation"] = delegation(client, victim)
    out["gaps"].extend(out["delegation"]["gaps"])
    ev = asset_events(client, victim, span)
    out["events"] = ev.items
    out["coverage"] = ev.to_dict()["coverage"]
    out["gaps"].extend(f"{g.kind}: {g.detail}" + (f" [{g.span.start}-{g.span.end}]" if g.span else "") for g in ev.gaps)
    events_complete = ev.coverage.complete and not any(g.kind == "unclassified" for g in ev.gaps)  # unclassified logs could be the drain
    def tv(found):
        return True if found else (False if events_complete else None)
    kinds = {e["event"] for e in ev.items}
    out["predicates"] = {
        "transfer_event_from_victim": tv("transfer_event_from_victim" in kinds),
        "approval_event_owner_victim": tv("approval_event_owner_victim" in kinds or "approval_for_all_event_owner_victim" in kinds),
        "delegation_observed_at_block": out["delegation"]["delegated"],
    }
    out["causal"] = {k: {"verdict": None, "reason": v} for k, v in UNKNOWN_CAUSES.items()}
    # per-token raw totals with decimals when known (never divided away: callers render)
    tokens = {}
    for e in ev.items:
        if e["event"] == "transfer_event_from_victim" and e["standard"] == "erc20":
            t = tokens.setdefault(e["contract"], {"out_raw": 0, "transfers": 0, "to": {}})
            t["out_raw"] += e["value_raw"]
            t["transfers"] += 1
            t["to"][e["to"]] = t["to"].get(e["to"], 0) + e["value_raw"]
    for i, (tk, t) in enumerate(tokens.items()):
        if i >= META_CAP:
            out["gaps"].append(f"{len(tokens)} tokens moved; metadata fetched for the first {META_CAP} only")
            break
        try:
            m = erc20_meta(client, tk)
        except (ApiError, BudgetExceeded):
            m = None
        t["symbol"], t["decimals"] = (m or {}).get("symbol"), (m or {}).get("decimals")
    out["tokens"] = tokens
    out["gaps"].extend(f"{g.kind}: {g.detail}" for g in STRUCTURAL_GAPS)
    # the run is complete only when every supported predicate is resolved: a failed delegation check leaves one unknown
    out["complete"] = events_complete and out["delegation"]["delegated"] is not None
    return out
