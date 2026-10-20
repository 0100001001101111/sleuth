"""Stage 1: urgent triage, RPC-first. Target: first useful result in <=20s even when the explorer is slow."""
import time
from concurrent.futures import ThreadPoolExecutor
from . import flows, launch, onchain
from .client import BudgetExceeded
from .labels import DEAD
from .watchlist import Watchlist


def launch_info(client, token):
    """Who launched the token and who received tokens in the creation tx. Explorer-only, cached (durable)."""
    meta = client.address(token)
    ctx = meta.get("creation_transaction_hash")
    info = {"creator": meta.get("creator_address_hash"), "creation_tx": ctx, "launcher": None, "initial_recipients": [],
            "creation_block": None, "launch_time": None}
    if not ctx:
        return info
    tx = client.tx(ctx)
    if not tx:
        return info
    info["launcher"] = (tx.get("from") or {}).get("hash")
    info["launch_time"] = tx.get("timestamp")
    info["creation_block"] = tx.get("block_number")
    for tt in tx.get("token_transfers") or []:
        tk = tt.get("token") or {}
        if (tk.get("address_hash") or tk.get("address") or "").lower() != token.lower():
            continue
        to = tt.get("to") or {}
        if to.get("is_contract") and to.get("proxy_type") != "eip7702":
            continue
        info["initial_recipients"].append({"addr": to.get("hash"), "amount": int((tt.get("total") or {}).get("value") or 0)})
    return info


def _dev_set(wl):
    return {a.lower() for a, e in wl.data["addresses"].items() if e["role"] in ("launcher", "dev", "initial_recipient")}


def run(client, token, window_s=1800, tracked=None, dev=None, top_n=5, from_block=None):
    """Returns a full result, or a partial one (gaps explain what is missing) if the budget runs out after the flow stage."""
    t0 = time.monotonic()
    out = {}
    try:
        return _run(client, token, window_s, tracked, dev, top_n, from_block, t0, out)
    except BudgetExceeded:
        if "flow" not in out:
            raise
        out["gaps"].append("budget exhausted: tracked balances / seller acquisition / ETH funding incomplete (rerun with --budget)")
        out.setdefault("tracked", []); out.setdefault("launch", {"launcher": None, "initial_recipients": []})
        out.setdefault("dev_sold_in_window", None)
        out["requests"], out["errors"], out["latency_s"] = client.requests, client.errors[:10], round(time.monotonic() - t0, 2)
        return out


def _run(client, token, window_s, tracked, dev, top_n, from_block, t0, out):
    token = token.lower()
    head = client.head()
    out.update({"stage": "triage", "chain_id": client.chain_id, "head": head, "token": token, "gaps": [], "timing": {}})
    if head["chain_id"] is not None and str(head["chain_id"]) != str(client.chain_id):
        out["gaps"].append(f"RPC chain id {head['chain_id']} != configured {client.chain_id}")
    if head["block"] is None:
        out["gaps"].append("RPC unreachable: no head block")
        out["summary"] = "RPC unreachable; cannot triage."
        return out
    tok = onchain.erc20_meta(client, token)
    if not tok:
        kinds = onchain.codes(client, [token])
        out["resolved_as"] = {"contract": "non-token contract", "eoa": "wallet", "eoa7702": "wallet (EIP-7702)"}.get(kinds.get(token), "unknown")
        out["summary"] = f"{token} is not an ERC-20 on chain {client.chain_id}; it is a {out['resolved_as']}. Use `trace` (wallet) or `contract`."
        return out
    dec = tok["decimals"]
    supply = tok.get("total_supply") or None
    out["token_meta"] = tok
    wl = Watchlist(client.chain_id, token)

    # Explorer identity lookups run in the background while RPC work proceeds (explorer is 2-10s/call).
    bg = ThreadPoolExecutor(max_workers=2)
    launch_fut = None
    if not (wl.data.get("launch") or {}).get("launcher"):
        li_rpc = launch.find(client, token, head["block"])  # one RPC call for launchpad tokens
        if li_rpc:
            wl.data["launch"] = li_rpc
        else:
            launch_fut = bg.submit(launch_info, client, token)  # explorer fallback (slow, cached)

    since = head["block_time"] - window_s
    if from_block is None:
        from_block = onchain.block_at_time(client, since, head["block"], head["block_time"])
    out["timing"]["setup_s"] = round(time.monotonic() - t0, 2)
    records = onchain.transfer_logs(client, token, from_block, head["block"])
    out["timing"]["logs_s"] = round(time.monotonic() - t0, 2)
    known_pools = set(wl.data.get("pools") or []) | {p.lower() for p in client.chain.get("known_pools", [])}
    if (wl.data.get("launch") or {}).get("market"):
        known_pools.add(wl.data["launch"]["market"])  # bonding-curve market: buys/sells before graduation
    pools, pool_hits, kinds = flows.detect_pools(records, lambda ad: onchain.codes(client, ad), known_pools)
    wl.data["pools"] = sorted(pools)
    tr = flows.trades(records, pools, DEAD)
    agg = flows.aggregate(tr)
    times, approx = onchain.block_times(client, [t["block"] for t in tr] + [from_block])
    for t in tr:
        t["ts"] = times.get(t["block"])
    pct = (lambda v: round(100 * v / supply, 3)) if supply else (lambda v: None)
    fmt = lambda v: round(v / 10 ** dec, 2)
    out["coverage"] = {"from_block": from_block, "to_block": head["block"], "from_time": times.get(from_block), "to_time": head["block_time"],
                       "transfers": len(records), "txs": len({r["tx"] for r in records}), "window_s": window_s, "complete": True,
                       "timestamps_approx": approx, "pools": sorted(pools), "pool_tx_hits": pool_hits}
    if not pools:
        out["gaps"].append("no pool detected: every move classified as plain transfer (pass known_pools in config)")

    sellers = sorted(((a, d) for a, d in agg.items() if d["sold"]), key=lambda kv: -kv[1]["sold"])
    kinds.update(onchain.codes(client, [a for a, _ in sellers[:top_n]]))
    out["flow"] = {"sold": fmt(sum(d["sold"] for d in agg.values())), "bought": fmt(sum(d["bought"] for d in agg.values())),
                   "burned": fmt(sum(d["burned"] for d in agg.values())), "sellers": len(sellers),
                   "buyers": sum(1 for d in agg.values() if d["bought"])}
    out["top_sellers"] = [{"addr": a, "kind": kinds.get(a, "unknown"), "sold": fmt(d["sold"]), "pct_supply": pct(d["sold"]), "trades": d["n"],
                           "sell_txs": d["txs"], "bought_in_window": fmt(d["bought"])} for a, d in sellers[:top_n]]
    out["burns"] = [{"addr": t["addr"], "amount": fmt(t["amount"]), "tx": t["tx"], "ts": t["ts"], "block": t["block"]}
                    for t in tr if t["kind"] == "burn"][:10]
    out["timing"]["flows_s"] = round(time.monotonic() - t0, 2)

    # --- launcher / dev wallets ---
    li = (wl.data.get("launch") or {"launcher": None, "initial_recipients": []}) if not launch_fut else None
    if launch_fut:
        try:
            li = launch_fut.result(timeout=max(0.5, min(8, client.remaining() or 8)))
        except Exception as e:  # noqa: BLE001 - explorer may be down; triage must still answer
            out["gaps"].append(f"launcher lookup failed/slow ({type(e).__name__}); pass --dev to track the developer")
            li = {"launcher": None, "initial_recipients": []}
    wl.data["launch"] = li
    if li.get("launcher"):
        wl.add(li["launcher"], "launcher", "sent token creation tx", [li.get("creation_tx")])
    for r in li.get("initial_recipients") or []:
        if r["addr"] and r["addr"].lower() != (li.get("launcher") or "").lower():
            wl.add(r["addr"], "initial_recipient", f"received {r['amount'] / 10 ** dec:,.0f} in creation tx", [li.get("creation_tx")])
    for a in tracked or []:
        wl.add(a, "tracked", "user supplied", [])
    for a in dev or []:
        wl.add(a, "dev", "user supplied as developer", [])
    dev_set = _dev_set(wl)
    out["launch"] = li

    watched = [(a, e) for a, e in wl.data["addresses"].items() if e["role"] in ("launcher", "dev", "initial_recipient", "tracked")][:20]
    bal = onchain.balances(client, token, [a for a, _ in watched])
    out["tracked"] = []
    for a, e in watched:
        d = agg.get(a, {})
        out["tracked"].append({"addr": a, "role": e["role"], "sold": fmt(d.get("sold", 0)), "bought": fmt(d.get("bought", 0)),
                               "burned": fmt(d.get("burned", 0)), "transferred_out": fmt(d.get("transferred_out", 0)),
                               "balance_now": fmt(bal[a]) if bal.get(a) is not None else None, "balance_fresh": bal.get(a) is not None,
                               "balance_pct": pct(bal[a]) if bal.get(a) is not None else None, "sell_txs": d.get("txs", [])})
    out["dev_sold_in_window"] = any(t["sold"] and t["addr"] in dev_set for t in out["tracked"])

    # --- top sellers: where did their tokens come from (RPC logs + receipts, full token history, routed hops netted) ---
    lo = li.get("creation_block") or max(0, head["block"] - 9_000_000)
    src_addrs = set()
    for s in out["top_sellers"]:
        a = s["addr"]
        try:
            ins = onchain.transfer_logs(client, token, lo, head["block"], to=a)
            recs = onchain.tx_records(client, list(dict.fromkeys(x["tx"] for x in reversed(ins)))[:25], token)
        except BudgetExceeded:
            out["gaps"].append("budget hit before seller acquisition check")
            break
        mine = [t for t in flows.trades(recs, pools, DEAD) if t["addr"] == a and t["kind"] in ("buy", "transfer_in")]
        srcs = {}
        for t in mine:
            k = "pool (buy)" if t["kind"] == "buy" else t["counterparty"]
            srcs[k] = srcs.get(k, 0) + t["amount"]
            if k.startswith("0x"):
                src_addrs.add(k)
        s["acquired_from"] = {k: fmt(v) for k, v in sorted(srcs.items(), key=lambda kv: -kv[1])[:4]}
        s["acquisition_txs"] = sorted({x["tx"] for x in ins})[:60]
        s["first_acquired_block"] = ins[0]["block"] if ins else None
        s["token_from_dev"] = sorted({t["counterparty"] for t in mine if t["counterparty"] in dev_set})
        s["dev_linked"] = bool(s["token_from_dev"]) or a in dev_set
    src_kinds = onchain.codes(client, sorted(src_addrs))
    for s in out["top_sellers"]:
        s["acquired_from_kinds"] = {k: src_kinds.get(k) for k in s.get("acquired_from", {}) if k.startswith("0x")}
    out["timing"]["acq_s"] = round(time.monotonic() - t0, 2)

    # --- ETH funders of top sellers: explorer internal txs, best effort, hard-capped so triage stays fast ---
    rem = client.remaining()
    cap = 8 if rem is None else min(8, rem - 1)
    if cap >= 3:
        tops = [s["addr"] for s in out["top_sellers"]]
        fund = client.many([f"/addresses/{a}/internal-transactions" for a in tops], timeout=cap, retries=0)
        for s, fd in zip(out["top_sellers"], fund):
            if fd is None:
                s["eth_funders_internal"] = None
                continue
            own = set(s.get("acquisition_txs") or []) | set(s["sell_txs"])
            funders = []
            for x in (fd.get("items") or []):
                if ((x.get("to") or {}).get("hash") or "").lower() != s["addr"] or int(x.get("value") or 0) <= 0:
                    continue
                if x.get("transaction_hash") in own:
                    continue  # ETH received inside the wallet's own swap = sale proceeds / refund, not funding
                f = x.get("from") or {}
                funders.append({"from": (f.get("hash") or "").lower(), "label": f.get("name"), "eth": round(int(x["value"]) / 1e18, 4),
                                "ts": x.get("timestamp"), "tx": x.get("transaction_hash"), "block": x.get("block_number"),
                                "pre_acquisition": bool(s.get("first_acquired_block") and (x.get("block_number") or 0) < s["first_acquired_block"])})
            s["eth_funders_internal"] = funders[:6]
            if any(f["from"] in dev_set for f in funders):
                s["dev_linked"] = True
                s["eth_from_dev"] = True
        if any(f is None for f in fund):
            out["gaps"].append("explorer slow: ETH funding of some top sellers not checked (run `trace`)")
    else:
        out["gaps"].append("budget: ETH funding of top sellers not checked (run `trace`)")

    if tr:
        wl.data["last_block"] = max(t["block"] for t in tr)
    for s in out["top_sellers"]:
        wl.add(s["addr"], "seller", f"sold {s['sold']:,} in triage window", s["sell_txs"])
    wl.save()
    bg.shutdown(wait=False)
    out["requests"] = client.requests
    out["errors"] = client.errors[:10]
    out["latency_s"] = round(time.monotonic() - t0, 2)
    return out
