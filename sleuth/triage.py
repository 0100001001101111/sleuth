"""Stage 1: urgent triage, RPC-first. Target: first useful result in <=20s even when the explorer is slow."""
import time
from concurrent.futures import ThreadPoolExecutor
from . import flows, launch, onchain
from .client import BudgetExceeded
from .onchain import RpcError
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


def _roles(e):
    return set(e.get("roles") or ([e["role"]] if e.get("role") else []))


def _dev_set(wl):
    """Launcher + dev only. A creation-tx recipient can be a bundled buyer or airdrop target, so initial_recipient
    is tracked and reported separately (_recip_set) but is NOT dev-linked evidence."""
    return {a.lower() for a, e in wl.data["addresses"].items() if _roles(e) & {"launcher", "dev"}}


def _recip_set(wl):
    return {a.lower() for a, e in wl.data["addresses"].items() if "initial_recipient" in _roles(e)}


def _dev_verdict(tracked, dev_set, dev_checked, logs_complete):
    """Three-valued: True on any dev sale; False only when the dev is identified, every dev wallet was actually
    checked, and the transfer-log coverage is complete; otherwise None (unknown). An empty dev set must never
    produce False -- that was the GH0ST hollow-verdict bug."""
    if any(t["sold"] for t in tracked if t["addr"] in dev_set):
        return True
    if dev_set and dev_checked and logs_complete:
        return False
    return None


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
    _lm = wl.data.get("launch") or {}
    if _lm.get("market") and not _lm.get("market_source"):
        # launch-event market: buys/sells before graduation. A first-mint-recipient market is a CANDIDATE only
        # (could be a vault/locker) and must earn pool status through flow shape / swap evidence.
        known_pools.add(_lm["market"])
    # Swap-event evidence lets a fresh, one-way market (e.g. sell-only in this window) still count as a venue.
    swap_topics = client.chain.get("swap_topics") or []
    unresolved_venues = set()

    def swap_check(addrs):
        spans = {}
        want = set(addrs[:8])
        for r in records:
            for a in (r["from"], r["to"]):
                if a in want:
                    mn, mx = spans.get(a, (r["block"], r["block"]))
                    spans[a] = (min(mn, r["block"]), max(mx, r["block"]))
        conf, unres = onchain.swap_evidence(client, addrs[:8], spans, swap_topics)
        unresolved_venues.update(unres)
        unresolved_venues.update(set(addrs) - want)  # candidates beyond the cap were never checked
        return conf

    pools, pool_hits, kinds = flows.detect_pools(records, lambda ad: onchain.codes(client, ad), known_pools, swap_check=swap_check)
    wl.data["pools"] = sorted(pools)
    tr = flows.trades(records, pools, DEAD)
    agg = flows.aggregate(tr)
    times, approx = onchain.block_times(client, [t["block"] for t in tr] + [from_block])
    for t in tr:
        t["ts"] = times.get(t["block"])
    pct = (lambda v: round(100 * v / supply, 3)) if supply else (lambda v: None)
    fmt = lambda v: round(v / 10 ** dec, 2)
    # 'Configured' pools are not 'active' pools: the PoolManager is always configured, so the warning must key on
    # whether any pool actually MOVED tokens in this window, or sells at an unknown venue pass silently as transfers.
    active_pools = sorted({p for p in pools if any(r["from"] == p or r["to"] == p for r in records)})
    out["coverage"] = {"from_block": from_block, "to_block": head["block"], "from_time": times.get(from_block), "to_time": head["block_time"],
                       "transfers": len(records), "txs": len({r["tx"] for r in records}), "window_s": window_s, "complete": True,
                       "timestamps_approx": approx, "pools": sorted(pools), "active_pools": active_pools, "pool_tx_hits": pool_hits}
    if records and not active_pools:
        out["gaps"].append("no pool ACTIVE in window: every move classified as plain transfer; a sale at an unknown venue would be missed")

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
            launch_fut.cancel()
            out["gaps"].append(f"launcher lookup failed/slow ({type(e).__name__}); pass --dev to track the developer")
            li = {"launcher": None, "initial_recipients": []}
    wl.data["launch"] = li
    if li.get("launcher"):
        src = li.get("launcher_source") or "creation tx"
        wl.add(li["launcher"], "launcher", f"sent token creation tx ({src})", [li.get("creation_tx")])
    for r in li.get("initial_recipients") or []:
        if r["addr"] and r["addr"].lower() != (li.get("launcher") or "").lower():
            wl.add(r["addr"], "initial_recipient", f"received {r['amount'] / 10 ** dec:,.0f} in creation tx", [li.get("creation_tx")])
    for a in tracked or []:
        wl.add(a, "tracked", "user supplied", [])
    for a in dev or []:
        wl.add(a, "dev", "user supplied as developer", [])
    dev_set = _dev_set(wl)
    recip_set = _recip_set(wl) - dev_set
    out["launch"] = li

    # Dev wallets first, so the 20-row cap can never silently exclude a developer; if it somehow does, the
    # verdict must say so instead of reading 'no selling'.
    prio = {"launcher": 0, "dev": 0, "initial_recipient": 1, "tracked": 2}
    watchable = sorted(((a, e) for a, e in wl.data["addresses"].items() if _roles(e) & set(prio)),
                       key=lambda ae: min(prio[r] for r in _roles(ae[1]) if r in prio))
    watched = watchable[:20]
    dev_checked = bool(dev_set) and dev_set <= {a for a, _ in watched}
    if dev_set and not dev_checked:
        out["gaps"].append("a dev wallet fell outside the 20-address tracking cap: dev verdict is 'unknown'")
    bal = onchain.balances(client, token, [a for a, _ in watched])
    out["tracked"] = []
    for a, e in watched:
        d = agg.get(a, {})
        out["tracked"].append({"addr": a, "role": e["role"], "roles": sorted(_roles(e)), "sold": fmt(d.get("sold", 0)),
                               "bought": fmt(d.get("bought", 0)),
                               "burned": fmt(d.get("burned", 0)), "transferred_out": fmt(d.get("transferred_out", 0)),
                               "balance_now": fmt(bal[a]) if bal.get(a) is not None else None, "balance_fresh": bal.get(a) is not None,
                               "balance_pct": pct(bal[a]) if bal.get(a) is not None else None, "sell_txs": d.get("txs", [])})
    out["dev_identified"] = bool(dev_set)
    out["dev_checked"] = dev_checked
    if not dev_set:
        out["gaps"].append("developer NOT identified: 'dev selling' cannot be answered (pass --dev, or run `sleuth dev-history`)")

    # --- top sellers: where did their tokens come from (RPC logs + receipts, full token history, routed hops netted) ---
    lo = li.get("creation_block") or max(0, head["block"] - 9_000_000)
    src_addrs = set()
    own_txs = {s["addr"]: {t["tx"] for t in tr if t["addr"] == s["addr"]} for s in out["top_sellers"]}  # every window trade
    acq_partial_gap = False
    for s in out["top_sellers"]:
        a = s["addr"]
        try:
            ins = onchain.transfer_logs(client, token, lo, head["block"], to=a)
            outs_s = onchain.transfer_logs(client, token, lo, head["block"], frm=a)
            uniq = list(dict.fromkeys(x["tx"] for x in reversed(ins)))
            recs = onchain.tx_records(client, uniq[:25], token)
        except BudgetExceeded:
            out["gaps"].append("budget hit before seller acquisition check")
            break
        except RpcError as e:
            out["gaps"].append(f"RPC failed during acquisition check of {a[:10]}: {e}")
            continue
        # full inbound AND outbound history: old sale proceeds must not read as 'funding' later
        own_txs[a] |= {x["tx"] for x in ins} | {x["tx"] for x in outs_s}
        s["acquisition_partial"] = len(uniq) > 25
        if s["acquisition_partial"] and not acq_partial_gap:
            out["gaps"].append("acquisition check partial for some sellers (only the 25 most recent inbound txs classified)")
            acq_partial_gap = True
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
        # Dev link from RAW transfer edges over the wallet's FULL inbound history: netting or the 25-receipt cap
        # must not erase a direct dev -> seller token movement. But a pure pass-through hop (in == out in the
        # same tx, net <= 0) acquired nothing and must not be marked dev-linked.
        net_tx = {}
        for x in ins:
            net_tx[x["tx"]] = net_tx.get(x["tx"], 0) + x["value"]
        for x in outs_s:
            net_tx[x["tx"]] = net_tx.get(x["tx"], 0) - x["value"]
        s["token_from_dev"] = sorted({x["from"] for x in ins if x["from"] in dev_set and net_tx.get(x["tx"], 0) > 0})
        s["token_from_launch_recipient"] = sorted({x["from"] for x in ins if x["from"] in recip_set and net_tx.get(x["tx"], 0) > 0})
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
            own = own_txs.get(s["addr"], set())
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

    if any(e.startswith("rpc") for e in client.errors):
        out["coverage"]["complete"] = False
        out["gaps"].append(f"{sum(e.startswith('rpc') for e in client.errors)} RPC call(s) failed; some receipts/timestamps may be missing")
    # A dev outflow in a TX that touches a contract whose venue status could not be resolved could be a hidden
    # sale (dev -> router -> unresolved pool: the direct counterparty is the router), so match at tx level.
    dev_out_txs = {t["tx"] for t in tr if t["addr"] in dev_set and t["kind"] == "transfer_out"}
    unres_txs = {r["tx"] for r in records if r["from"] in unresolved_venues or r["to"] in unresolved_venues}
    dev_outflow_unresolved = bool(dev_out_txs & unres_txs)
    if unresolved_venues:
        out["gaps"].append(f"venue status UNRESOLVED for {len(unresolved_venues)} contract(s) (swap-evidence query failed): "
                           "outflows there are classified as transfers" + (" — dev verdict degraded" if dev_outflow_unresolved else ""))
    # A venue too fresh to be detected (<3 txs) still leaves a Swap event in the dev outflow's own receipt: check
    # those receipts directly (cheap, cached) so a dev's single sell into a brand-new pool cannot read as a transfer.
    dev_swap_hint = False
    if dev_out_txs and swap_topics:
        recs_d, topics_d = onchain.tx_records_full(client, sorted(dev_out_txs)[:10], token)
        touch = {}
        for r in recs_d:
            touch.setdefault(r["tx"], set()).update((r["from"], r["to"]))
        for h, emap in topics_d.items():
            if set().union(*(set(emap.get(tp) or []) for tp in swap_topics)) & touch.get(h, set()):
                dev_swap_hint = True
        if dev_swap_hint:
            out["gaps"].append("a dev outflow's receipt contains a swap event by a contract the token touched, but the "
                               "venue was not recognized: possible sale at a fresh venue — dev verdict degraded")
        if len(dev_out_txs) > 10:
            dev_swap_hint = True
            out["gaps"].append(f"{len(dev_out_txs)} dev outflow txs; only 10 receipt-checked for hidden venues — dev verdict degraded")
    # A clean window with NO active venue at all cannot prove 'no dev sale' if the dev moved tokens out: the sale
    # could have happened at a venue this run cannot see.
    blind_window = bool(records) and not active_pools and bool(dev_out_txs)
    # A candidate-only launcher identity (first-mint signer, no --dev) cannot support a firm 'no'.
    identity_firm = bool(dev) or not (li.get("launcher_source") or "").startswith("creation_tx_signer")
    if not identity_firm and dev_set:
        out["gaps"].append("launcher identified from creation-tx signer only (may be a relayer or launch service): "
                           "a clean window is reported as unknown, not 'none'")
    out["completeness"] = {"dev_identity": bool(dev_set), "dev_identity_firm": identity_firm, "dev_checked": dev_checked,
                           "transfer_logs": out["coverage"]["complete"], "venue_active": bool(active_pools),
                           "venues_resolved": not unresolved_venues}
    # raw (unrounded) sold amounts: a dust sale must not round to 0.00 and read as 'no sale'
    raw_rows = [{"addr": a, "sold": agg.get(a, {}).get("sold", 0)} for a, _ in watched]
    out["dev_sold_in_window"] = _dev_verdict(raw_rows, dev_set, dev_checked,
                                             out["coverage"]["complete"] and not dev_outflow_unresolved
                                             and not dev_swap_hint and not blind_window and identity_firm)
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
