"""Full-life developer check, RPC-first: every token movement of the launcher/dev wallets since token creation,
classified with receipt-level swap evidence. Answers "has the dev EVER sold?" -- three-valued, never a false negative:
a sell is only CONFIRMED by a swap event in its own receipt; a venue outflow without one could be an LP add, so it
degrades the verdict to unknown instead of being counted either way."""
import time
from . import devprofile, flows, launch, onchain
from .client import BudgetExceeded
from .labels import DEAD
from .watchlist import Watchlist

RECEIPTS_CAP = 150


def run(client, token, dev=None, receipts_cap=RECEIPTS_CAP):
    t0 = time.monotonic()
    out = {"stage": "dev-history", "chain_id": client.chain_id, "token": token.lower(), "gaps": [], "wallets": []}
    try:
        return _run(client, token.lower(), dev, receipts_cap, t0, out)
    except BudgetExceeded:
        out["gaps"].append("budget exhausted mid-run: history incomplete (rerun with --budget)")
        out["dev_ever_sold"] = True if any(w.get("swap_confirmed_sells") for w in out["wallets"]) else None
        out["requests"], out["errors"], out["latency_s"] = client.requests, client.errors[:10], round(time.monotonic() - t0, 2)
        return out


def _verdict(wallets, complete):
    """True on any swap-confirmed sell; False only when every wallet's full history was read and nothing even
    ambiguous was found; otherwise None."""
    if any(w["swap_confirmed_sells"] for w in wallets):
        return True
    if complete and not any(w["unconfirmed_venue_outflows"] for w in wallets):
        return False
    return None


def _run(client, token, dev, receipts_cap, t0, out):
    head = client.head()
    out["head"] = head
    if head["block"] is None:
        out["gaps"].append("RPC unreachable: no head block")
        out["dev_ever_sold"] = None
        out["requests"], out["errors"], out["latency_s"] = client.requests, client.errors[:10], round(time.monotonic() - t0, 2)
        return out
    tok = onchain.erc20_meta(client, token)
    if not tok:
        out["summary"] = f"{token} is not an ERC-20 on chain {client.chain_id}"
        out["dev_ever_sold"] = None
        out["requests"], out["errors"], out["latency_s"] = client.requests, client.errors[:10], round(time.monotonic() - t0, 2)
        return out
    dec = tok["decimals"]
    fmt = lambda v: round(v / 10 ** dec, 4)
    out["token_meta"] = tok
    wl = Watchlist(client.chain_id, token)

    li = wl.data.get("launch") or {}
    if not li.get("launcher"):
        li = launch.find(client, token, head["block"]) or li
        if li:
            wl.data["launch"] = li
    out["launch"] = li
    if li.get("launcher"):
        wl.add(li["launcher"], "launcher", f"sent token creation tx ({li.get('launcher_source') or 'creation tx'})",
               [li.get("creation_tx")])
    for a in dev or []:
        wl.add(a, "dev", "user supplied as developer", [])
    # persisted dev roles from earlier runs count too: a saved second dev wallet must not be skipped
    dev_set = sorted({a.lower() for a in (dev or [])} | ({li["launcher"].lower()} if li.get("launcher") else set())
                     | set(wl.by_role("launcher", "dev")))
    out["dev_wallets"] = dev_set
    if not dev_set:
        out["gaps"].append("developer NOT identified (no launch event, no first mint found, no --dev): verdict is unknown")
        out["dev_ever_sold"] = None
        wl.save()
        out["requests"], out["errors"], out["latency_s"] = client.requests, client.errors[:10], round(time.monotonic() - t0, 2)
        return out
    identity_firm = bool(dev) or not (li.get("launcher_source") or "").startswith("creation_tx_signer")
    if not identity_firm:
        out["gaps"].append("launcher identified from creation-tx signer only (may be a relayer or launch service): "
                           "a clean history is reported as unknown, not NEVER")

    window_full = li.get("creation_block") is not None
    lo = li.get("creation_block") or max(0, head["block"] - onchain.MAX_RANGE)
    if not window_full:
        out["gaps"].append(f"token creation block unknown: history covers only the last {onchain.MAX_RANGE:,} blocks, "
                           "not the token's full life")
    known_pools = set(wl.data.get("pools") or []) | {p.lower() for p in client.chain.get("known_pools", [])}
    if li.get("market") and not li.get("market_source"):
        # launch-event markets are proven venues; a first-mint-recipient market is a CANDIDATE (could be a vault
        # or locker) and must earn pool status through flow shape / swap evidence like any other contract
        known_pools.add(li["market"].lower())
    swap_topics = client.chain.get("swap_topics") or []

    complete = True
    for a in dev_set[:4]:
        w = {"addr": a, "events": [], "swap_confirmed_sells": [], "unconfirmed_venue_outflows": [], "complete": True}
        out["wallets"].append(w)
        ins = onchain.transfer_logs(client, token, lo, head["block"], to=a)
        outs = onchain.transfer_logs(client, token, lo, head["block"], frm=a)
        ordered = sorted(ins + outs, key=lambda x: (x["block"], x["idx"]))
        # Txs where the wallet SENDS tokens are the candidate sells: classify them first, so a receipts cap can
        # only ever drop inbound-only txs -- it must never hide a sell.
        out_txs = list(dict.fromkeys(x["tx"] for x in sorted(outs, key=lambda x: (x["block"], x["idx"]))))
        in_txs = [h for h in dict.fromkeys(x["tx"] for x in ordered) if h not in set(out_txs)]
        tx_list = (out_txs + in_txs)[:receipts_cap]
        if len(out_txs) + len(in_txs) > receipts_cap:
            w["complete"] = False
            if len(out_txs) <= receipts_cap:
                out["gaps"].append(f"{a[:10]}: {len(out_txs) + len(in_txs)} txs > receipts cap {receipts_cap}; "
                                   f"all {len(out_txs)} outbound (candidate-sell) txs classified, some inbound skipped")
            else:
                out["gaps"].append(f"{a[:10]}: {len(out_txs)} outbound txs exceed the receipts cap {receipts_cap}; "
                                   "sell history itself is incomplete")
        n_err = len(client.errors)
        recs, topics = onchain.tx_records_full(client, tx_list, token)
        lost = sum(1 for e in client.errors[n_err:] if e.startswith("rpc receipt missing"))
        if lost:
            w["complete"] = False
            out["gaps"].append(f"{a[:10]}: {lost} receipt(s) missing; classification incomplete")
        unresolved_venues = set()

        def swap_check(addrs):
            spans = {}
            want = set(addrs[:8])
            for r in recs:
                for x in (r["from"], r["to"]):
                    if x in want:
                        mn, mx = spans.get(x, (r["block"], r["block"]))
                        spans[x] = (min(mn, r["block"]), max(mx, r["block"]))
            conf, unres = onchain.swap_evidence(client, addrs[:8], spans, swap_topics)
            unresolved_venues.update(unres)
            unresolved_venues.update(set(addrs) - want)  # candidates beyond the cap were never checked
            return conf

        pools, _, _ = flows.detect_pools(recs, lambda ad: onchain.codes(client, ad), known_pools, swap_check=swap_check)
        times, approx = onchain.block_times(client, [r["block"] for r in recs])
        tx_touch = {}  # tx -> every address this token's transfers touched in that tx
        for r in recs:
            tx_touch.setdefault(r["tx"], set()).update((r["from"], r["to"]))

        def _swap_emitters(h):
            emap = topics.get(h) or {}
            return set().union(*(set(emap.get(tp) or []) for tp in swap_topics)) if swap_topics else set()

        raw = {"sold_confirmed": 0, "venue_outflow": 0, "burned": 0, "bought": 0, "transferred_out": 0, "transferred_in": 0}
        for t in flows.trades(recs, pools, DEAD):
            if t["addr"] != a:
                continue
            ev = {"tx": t["tx"], "block": t["block"], "ts": times.get(t["block"]), "kind": t["kind"],
                  "amount": fmt(t["amount"]), "counterparty": t["counterparty"]}
            if t["kind"] == "sell":
                # confirmation needs a swap event EMITTED BY a venue this token's flow touched in this tx -- a swap
                # of an unrelated token elsewhere in the multicall must not confirm an LP add as a sale
                if _swap_emitters(t["tx"]) & tx_touch.get(t["tx"], set()) & pools:
                    ev["kind"] = "sell (swap-confirmed)"
                    w["swap_confirmed_sells"].append(t["tx"])
                    raw["sold_confirmed"] += t["amount"]
                else:
                    ev["kind"] = "venue outflow (sell or LP add; no swap event decoded)"
                    w["unconfirmed_venue_outflows"].append(t["tx"])
                    raw["venue_outflow"] += t["amount"]
            elif t["kind"] == "transfer_out" and _swap_emitters(t["tx"]) & tx_touch.get(t["tx"], set()):
                # a venue too fresh to be detected (<3 txs): the receipt itself shows a swap by a token-touching
                # contract, so this outflow may be a sale -- degrade, never count it as a plain transfer
                ev["kind"] = "venue outflow (swap event in tx; venue not recognized)"
                w["unconfirmed_venue_outflows"].append(t["tx"])
                raw["venue_outflow"] += t["amount"]
            elif t["kind"] == "burn":
                raw["burned"] += t["amount"]
            elif t["kind"] == "buy":
                raw["bought"] += t["amount"]
            elif t["kind"] == "transfer_out":
                raw["transferred_out"] += t["amount"]
            elif t["kind"] == "transfer_in":
                raw["transferred_in"] += t["amount"]
            w["events"].append(ev)
        w["events"].sort(key=lambda e: (e["block"], e["tx"]))
        # An outflow in a tx that touches ANY unresolved-status contract could be a hidden sale (dev -> router ->
        # unresolved pool: the direct counterparty is the router), so match at tx level, not counterparty level.
        unres_txs = {h for h, touch in tx_touch.items() if touch & unresolved_venues}
        hit = [e for e in w["events"] if e["kind"] == "transfer_out" and e["tx"] in unres_txs]
        if hit:
            w["complete"] = False
            out["gaps"].append(f"{a[:10]}: outflow to contract(s) whose venue status could not be resolved "
                               "(swap-evidence query failed) — could be a hidden sale")
        w["timestamps_approx"] = approx
        bal = onchain.balances(client, token, [a]).get(a)
        w["balance_now"] = fmt(bal) if bal is not None else None
        w["totals"] = {k: fmt(v) for k, v in raw.items()}
        complete = complete and w["complete"]
    if len(dev_set) > 4:
        complete = False
        out["gaps"].append(f"{len(dev_set)} dev wallets; only the first 4 checked")
    if any(e.startswith("rpc") for e in client.errors):
        complete = False
        out["gaps"].append(f"{sum(e.startswith('rpc') for e in client.errors)} RPC call(s) failed: history incomplete")
    # a candidate-only identity or an unknown creation block can never support a firm NEVER
    complete = complete and identity_firm and window_full
    out["history_complete"] = complete
    out["dev_ever_sold"] = _verdict(out["wallets"], complete)
    # Creator-fee claims: a dev that never sold can still be extracting ETH through a fee escrow, and a
    # "NEVER sold" headline without that number is misleading. Reporting only, AFTER the verdict is committed:
    # an explorer stall or budget exhaustion here must only leave fee_claims unknown, never touch the verdict.
    for w in out["wallets"]:
        try:
            page = client.get(f"/addresses/{w['addr']}/internal-transactions", None, True, 6, 0)
        except BudgetExceeded:
            page = None
        w["fee_claims"] = devprofile.fee_claims(page, w["addr"], client.chain, client.cfg)
        if w["fee_claims"] is None:
            out["gaps"].append(f"{w['addr'][:10]}: fee-claim check failed (explorer slow/budget): fee extraction unknown")
    wl.save()
    out["requests"] = client.requests
    out["errors"] = client.errors[:10]
    out["latency_s"] = round(time.monotonic() - t0, 2)
    return out
