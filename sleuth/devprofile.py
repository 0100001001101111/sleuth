"""Developer-wallet profile from one explorer internal-ETH page: creator-fee extraction and pre-launch funding.

"Dev never sold" is not the whole story: a launch-farm dev burns its buy and harvests creator fees instead
(V4M 2026-10-22: launcher burned all 16.6M tokens yet claimed 3.3+ ETH from PonsV2FeeEscrow, and was itself
batch-funded via GasliteDrop 4 minutes before launch). Three-valued like every verdict input: a missing page
is unknown, never "no claims" / "no farm signals"."""
from datetime import datetime

BATCH_MARKERS = ("batchtransfer", "disperse", "multisend", "multisender", "batch", "gaslite", "airdrop")
FRESH_FUNDING_MINUTES = 60


def _h(o):
    return ((o or {}).get("hash") or "").lower()


def _ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)  # numeric string (some explorers return unix ts as a string)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _incoming(page, addr):
    """Successful internal ETH credits to addr. Reverted calls moved nothing and must not become evidence."""
    a = addr.lower()
    for x in page.get("items") or []:
        if _h(x.get("to")) != a or int(x.get("value") or 0) <= 0:
            continue
        if x.get("error") or not x.get("success", True):
            continue
        yield x


def fee_claims(page, addr, chain_cfg, cfg):
    """ETH the wallet received from fee-escrow contracts (creator-fee extraction). None = unknown (page missing);
    a paged result with nothing found stays honest via partial=True, it is never proof of 'no claims'."""
    if page is None:
        return None
    escrows = {e.lower() for e in chain_cfg.get("fee_escrows") or []}
    markers = cfg.get("fee_claim_markers") or []
    eth, n, latest, sources = 0.0, 0, None, {}
    for x in _incoming(page, addr):
        f = x.get("from") or {}
        fh, name = _h(f), f.get("name") or ""
        if fh in escrows or any(m in name.lower() for m in markers):
            eth += int(x["value"]) / 1e18
            n += 1
            sources[fh] = name or sources.get(fh) or "fee escrow"
            ts = x.get("timestamp")
            if ts and (latest is None or ts > latest):
                latest = ts
    return {"eth": round(eth, 4), "n": n, "sources": sources, "latest": latest,
            "partial": bool(page.get("next_page_params"))}


def launch_funding(client, page, addr, creation_block=None, launch_time=None):
    """Who funded the launcher BEFORE it created the token, with launch-farm signals: funding minutes before
    launch, or via a batch-airdrop contract. None = unknown (page or creation_block missing). Internal transfers
    only: a plain EOA top-level funding is invisible here, so absence of events is never 'not funded'."""
    if page is None or creation_block is None:
        return None  # without the creation boundary, pre-launch cannot be separated from fee income: unknown
    events = []
    for x in _incoming(page, addr):
        blk = x.get("block_number")
        if blk is None or blk >= creation_block:
            continue
        f = x.get("from") or {}
        events.append({"from": _h(f), "label": f.get("name"), "eth": round(int(x["value"]) / 1e18, 5),
                       "ts": x.get("timestamp"), "block": blk, "tx": x.get("transaction_hash")})
    events.sort(key=lambda e: e["block"] or 0)
    out = {"events": events[:4], "partial": bool(page.get("next_page_params")), "signals": [], "batch": None,
           "minutes_before_launch": None}
    if not events:
        return out
    first = events[0]
    lt, ft = _ts(launch_time), _ts(first.get("ts"))
    # a first-ever funding (internal view) minutes before launch marks a fresh wallet; a contradictory
    # post-launch timestamp is inconsistent evidence and produces no signal, never a "0 min" one
    if lt and ft and ft <= lt:
        mins = (lt - ft) / 60
        out["minutes_before_launch"] = round(mins, 1)
        if mins <= FRESH_FUNDING_MINUTES:
            out["signals"].append(f"launcher funded {first['eth']} ETH only {mins:.0f} min before launch "
                                  "(fresh disposable wallet)")
    batch_ev = next((e for e in events if any(m in (e.get("label") or "").lower() for m in BATCH_MARKERS)), None)
    if batch_ev:
        n_rec = None
        try:
            n_rec = len({_h(it.get("to")) for it in client.tx_internal(batch_ev["tx"]) or []
                         if int(it.get("value") or 0) > 0 and not it.get("error") and it.get("success", True)})
        except Exception:  # noqa: BLE001 - recipient count is color, never worth failing the run
            pass
        out["batch"] = {"tx": batch_ev["tx"], "via": batch_ev.get("label"), "recipients": n_rec}
        out["signals"].append(f"funded via {batch_ev['label']} batch"
                              + (f" with {n_rec} recipients" if n_rec else "") + " — launch-farm pattern")
    return out
