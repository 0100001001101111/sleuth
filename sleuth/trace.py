"""Stage 2: focused connection tracing for specific wallets.

Separate verdicts, never merged:
  direct_tx_connection  - A sent value to B (or vice versa) in a transaction
  shared_funding        - the same non-service address funded both
  coordinated           - batch-funded siblings that bought the same token in the same block(s)
  dev_connection        - any value path to the known developer / launcher set
  ownership             - always 'unknown' from chain data alone
Token moves come from RPC logs (fast); ETH funding / internal transfers need the explorer (slow, parallelised per wallet).
"""
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from . import flows, onchain
from .client import BudgetExceeded
from .labels import classify, DEAD

BATCH_MARKERS = ("batchtransfer", "disperse", "multisend", "multisender", "batch")


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else 0


def _h(o):
    return ((o or {}).get("hash") or "").lower()


def _label(client, addr, cache):
    a = addr.lower()
    if a not in cache:
        meta = client.address(addr)
        cnt = client.counters(addr) if not meta.get("is_contract") else None
        cache[a] = classify(meta, cnt, client.cfg) + (meta,)
    return cache[a]


def wallet(client, addr, token, dev_set, labels, pools, head, lo_block):
    a = addr.lower()
    kind, label, meta = _label(client, addr, labels)
    r = {"addr": a, "kind": kind, "label": label, "token_acquisitions": [], "token_disposals": [],
         "eth_funding": [], "eth_in_own_token_txs": [], "batch_siblings": [], "proceeds_out": [], "dev_links": [], "complete": True}
    ins = onchain.transfer_logs(client, token, lo_block, head["block"], to=a)
    outs = onchain.transfer_logs(client, token, lo_block, head["block"], frm=a)
    ordered = sorted(ins + outs, key=lambda x: (x["block"], x["idx"]))
    tx_hashes = list(dict.fromkeys(x["tx"] for x in ordered))  # block-ordered, deterministic
    if len(tx_hashes) > 200:
        r["gaps"] = [f"{len(tx_hashes)} token txs; only the first 200 (block-ordered) classified"]
        r["complete"] = False
        tx_hashes = tx_hashes[:200]
    own_txs = {x["tx"] for x in ins + outs}  # every tx that moved this token for the wallet: ETH received inside = proceeds/refund
    # Net every tx at receipt level so wallet -> router -> pool counts as one sell, router hops ignored
    n_err = len(client.errors)
    recs = onchain.tx_records(client, tx_hashes, token)
    lost = sum(1 for e in client.errors[n_err:] if e.startswith("rpc receipt missing"))
    if lost:
        r["complete"] = False
        r.setdefault("gaps", []).append(f"{lost} receipt(s) unavailable: token classification incomplete")
    mine = {t["tx"]: t for t in flows.trades(recs, pools, DEAD) if t["addr"] == a}
    times, _ = onchain.block_times(client, [x["block"] for x in ins + outs])
    seen = set()
    for x in ins + outs:
        if x["tx"] in seen:
            continue
        seen.add(x["tx"])
        t = mine.get(x["tx"])
        if not t:
            continue
        rec = {"ts": times.get(x["block"]), "block": x["block"], "amount": t["amount"], "tx": x["tx"]}
        # `counterparties` holds every direct edge party: a dust input from a second wallet must not erase a dev link
        if t["kind"] in ("buy", "transfer_in"):
            rec["from"] = t["counterparty"] if t["kind"] == "transfer_in" else None
            rec["via"] = "pool (buy)" if t["kind"] == "buy" else "transfer"
            froms = set(t.get("counterparties") or ([rec["from"]] if rec["from"] else []))
            if len(froms) > 1:
                rec["from_all"] = sorted(froms)
            if froms & dev_set:
                r["dev_links"].append({"type": "token_from_dev", **rec})
            r["token_acquisitions"].append(rec)
        else:
            rec["to"] = t["counterparty"] if t["kind"] == "transfer_out" else None
            rec["via"] = {"sell": "pool (sell)", "burn": "burn"}.get(t["kind"], "transfer")
            tos = set(t.get("counterparties") or ([rec["to"]] if rec["to"] else []))
            if len(tos) > 1:
                rec["to_all"] = sorted(tos)
            if tos & dev_set:
                r["dev_links"].append({"type": "token_to_dev", **rec})
            r["token_disposals"].append(rec)
    first_acq = min((x["ts"] for x in r["token_acquisitions"] if x["ts"]), default=None)

    # ETH funding: plain txs + internal transfers (batch distributions arrive as internal) -- explorer
    txs, c1 = client.paginate(f"/addresses/{addr}/transactions", max_pages=1)
    its, c2 = client.paginate(f"/addresses/{addr}/internal-transactions", max_pages=1)
    if not (c1 and c2):
        r["complete"] = False
        r.setdefault("gaps", []).append("explorer ETH history truncated/unavailable (first page only)")
    for x in txs:
        if _h(x.get("to")) == a and int(x.get("value") or 0) > 0 and x.get("status") == "ok":
            e = {"ts": x["timestamp"], "from": _h(x.get("from")), "eth": int(x["value"]) / 1e18, "tx": x["hash"], "via": None}
            (r["eth_in_own_token_txs"] if x["hash"] in own_txs else r["eth_funding"]).append(e)
    for x in its:
        if _h(x.get("to")) == a and int(x.get("value") or 0) > 0 and not x.get("error") and x.get("success", True):
            e = {"ts": x["timestamp"], "from": _h(x.get("from")), "eth": int(x["value"]) / 1e18,
                 "tx": x["transaction_hash"], "via": (x.get("from") or {}).get("name"), "internal": True}
            (r["eth_in_own_token_txs"] if x["transaction_hash"] in own_txs else r["eth_funding"]).append(e)
    r["eth_funding"].sort(key=lambda e: e["ts"])
    for e in r["eth_funding"]:
        k, lab, _m = _label(client, e["from"], labels)
        e["from_kind"], e["from_label"] = k, lab
        e["pre_acquisition"] = bool(first_acq and _ts(e["ts"]) <= first_acq)
        if e["from"] in dev_set:
            r["dev_links"].append({"type": "eth_from_dev", **e})
    pre = [e for e in r["eth_funding"] if e["pre_acquisition"]]
    r["funding_before_acquisition"] = pre[-1] if pre else None

    # Batch distributions: internal funding via a batch contract -> enumerate every recipient. A fresh UNVERIFIED
    # contract has no name to match, so unnamed internal distributors are enumerated too and count as a batch
    # when the same tx paid out to other recipients (name matching alone is trivially evaded).
    for e in (pre[-3:] if pre else r["eth_funding"][-2:]):
        via = (e.get("via") or "").lower()
        named_batch = any(m in via for m in BATCH_MARKERS)
        if named_batch or (e.get("internal") and not via):
            ptx = client.tx(e["tx"])
            sender = _h((ptx or {}).get("from"))
            recips = [{"addr": _h(it.get("to")), "eth": int(it["value"]) / 1e18} for it in client.tx_internal(e["tx"])
                      if int(it.get("value") or 0) > 0 and _h(it.get("to")) != a and not it.get("error") and it.get("success", True)]
            if not named_batch and len(recips) < 2:
                continue
            r["batch_siblings"].append({"tx": e["tx"], "ts": e["ts"], "sender": sender, "contract": e.get("via"),
                                        "recipients": recips, "my_eth": e["eth"]})
            if sender in dev_set:
                r["dev_links"].append({"type": "batch_from_dev", "tx": e["tx"], "sender": sender})

    # Sale proceeds (gross ETH received in sell txs) and where ETH went after the last sell
    sell_txs = {x["tx"] for x in r["token_disposals"] if x["via"] == "pool (sell)"}
    gross = sum(int(x.get("value") or 0) for x in its if x.get("transaction_hash") in sell_txs and _h(x.get("to")) == a)
    r["gross_sale_proceeds_eth"] = round(gross / 1e18, 6)
    last_sell = max((x["ts"] for x in r["token_disposals"] if x["via"] == "pool (sell)" and x["ts"]), default=None)
    for x in txs:
        if _h(x.get("from")) == a and int(x.get("value") or 0) > 0 and (not last_sell or _ts(x["timestamp"]) >= last_sell):
            to = _h(x.get("to"))
            k, lab, _m = _label(client, to, labels) if to else ("unknown", "?", {})
            r["proceeds_out"].append({"ts": x["timestamp"], "to": to, "to_kind": k, "to_label": lab, "eth": int(x["value"]) / 1e18,
                                      "tx": x["hash"], "method": x.get("method"), "resolved": k == "eoa"})
            if to in dev_set:
                r["dev_links"].append({"type": "eth_to_dev", "tx": x["hash"], "eth": int(x["value"]) / 1e18})
    return r


def group(client, addrs, token, dev_set, pools=(), lo_block=None):
    t0 = time.monotonic()
    labels = {}
    dev_set = {d.lower() for d in dev_set}
    pools = {p.lower() for p in pools}
    head = client.head()
    lo = lo_block or max(0, (head["block"] or 0) - 9_000_000)
    traces, gaps, incomplete = [], [], False
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(addrs)))) as ex:
        futs = {ex.submit(wallet, client, a, token, dev_set, labels, pools, head, lo): a for a in addrs}
        for f, a in futs.items():
            try:
                traces.append(f.result())
            except BudgetExceeded:
                gaps.append(f"budget exhausted before tracing {a}")
                incomplete = True
            except Exception as e:  # noqa: BLE001 - one wallet failing must not lose the others
                gaps.append(f"{a}: {type(e).__name__} {str(e)[:80]}")
                incomplete = True
    traces.sort(key=lambda t: [x.lower() for x in addrs].index(t["addr"]))
    for t in traces:
        gaps.extend(f"{t['addr']}: {g}" for g in t.pop("gaps", []))
        incomplete = incomplete or not t["complete"]
    members = {a.lower() for a in addrs}  # requested wallets count as members even if their own trace ran out of budget

    # Shared funding = the same *wallet* (EOA) sent ETH to >1 member before they acquired the token.
    # Pools, routers, bridges and unlabelled contracts are services: reported separately, never as a common owner.
    funders = defaultdict(set)
    for t in traces:
        for e in t["eth_funding"]:
            if e["from_kind"] == "eoa" and e["pre_acquisition"] and e["from"] not in members and e["from"] not in pools:
                funders[e["from"]].add(t["addr"])
    shared = [{"funder": f, "label": labels.get(f, ("", "", {}))[1], "members": sorted(m)} for f, m in funders.items() if len(m) > 1]
    # A common funder whose kind could not be resolved (explorer miss) may still be a shared wallet:
    # it must make the negative verdict unknown, never silently drop out of both buckets.
    unclassified = defaultdict(set)
    for t in traces:
        for e in t["eth_funding"]:
            if e["from_kind"] == "unknown" and e["pre_acquisition"] and e["from"] not in members:
                unclassified[e["from"]].add(t["addr"])
    shared_unclassified = [{"funder": f, "members": sorted(m)} for f, m in unclassified.items() if len(m) > 1]
    svc = defaultdict(set)
    for t in traces:
        for e in t["eth_funding"]:
            if e["from_kind"] in ("service", "contract") or e["from"] in pools:
                svc[(e["from"], e["from_label"] if e["from_kind"] == "service" else f"{e['from_label']} (contract)")].add(t["addr"])
    service_only = [{"service": s, "label": l, "members": sorted(m)} for (s, l), m in svc.items() if len(m) > 1]

    direct = []
    for t in traces:
        # a wallet moving ETH/tokens to ITSELF is not a member-to-member connection: self rows here made
        # "direct tx link: YES" fire on nothing but self-transfers
        for e in t["eth_funding"]:
            if e["from"] in members and e["from"] != t["addr"]:
                direct.append({"from": e["from"], "to": t["addr"], "eth": e["eth"], "tx": e["tx"]})
        for b in t["batch_siblings"]:
            if b["sender"] in members and b["sender"] != t["addr"]:
                direct.append({"from": b["sender"], "to": t["addr"], "eth": b["my_eth"], "tx": b["tx"], "via": b["contract"]})
        for x in t["token_acquisitions"]:
            for f in (x.get("from_all") or ([x["from"]] if x.get("from") else [])):
                if f in members and f != t["addr"]:
                    direct.append({"from": f, "to": t["addr"], "token_amount": x["amount"], "tx": x["tx"]})

    coord, seen_batches = [], set()
    for t in traces:
        for b in t["batch_siblings"]:
            if b["tx"] in seen_batches:
                continue
            seen_batches.add(b["tx"])
            sib = {r["addr"] for r in b["recipients"]} | {t["addr"]}
            blocks = defaultdict(set)
            for u in traces:
                if u["addr"] in sib:
                    for x in u["token_acquisitions"]:
                        if x["via"] == "pool (buy)":
                            blocks[x["block"]].add(u["addr"])
            same = {bl: sorted(m) for bl, m in blocks.items() if len(m) > 1}
            coord.append({"batch_tx": b["tx"], "sender": b["sender"], "siblings": sorted(sib), "same_block_buys": same,
                          "untraced_siblings": sorted(sib - members),
                          "amounts_eth": sorted({round(r["eth"], 9) for r in b["recipients"]} | {round(b["my_eth"], 9)})})
    dev_links = [{"addr": t["addr"], **l} for t in traces for l in t["dev_links"]]
    # Positive evidence is True. A negative is only False when every evidence source was complete; otherwise None (unknown).
    neg = False if not incomplete else None
    verdict = {
        "direct_tx_connection": bool(direct) or neg,
        "shared_funding": bool(shared) or (None if shared_unclassified else neg),
        "shared_funder_unclassified": shared_unclassified,
        "shared_service_only": sorted(service_only, key=lambda s: s["service"]),
        "coordinated_trading": any(c["same_block_buys"] for c in coord) or neg,
        "batch_funded": bool(coord) or neg,
        "dev_connection": bool(dev_links) or neg,
        "complete": not incomplete,
        "ownership": "unknown (chain data cannot prove a human owner)",
    }
    if shared_unclassified:
        gaps.append(f"{len(shared_unclassified)} common funder(s) unresolved (explorer miss): shared_funding is 'unknown' until classified")
    if incomplete:
        gaps.append("evidence incomplete: negative verdicts are 'unknown', not 'no'")
    try:
        token_meta = onchain.erc20_meta(client, token)
    except Exception:  # noqa: BLE001 - decimals are presentation only, never block the verdict
        token_meta = None
    return {"stage": "trace", "chain_id": client.chain_id, "token": token, "head": head, "token_meta": token_meta,
            "wallets": traces, "direct": direct,
            "shared_funders": shared, "coordinated": coord, "dev_links": dev_links, "verdict": verdict, "gaps": gaps,
            "requests": client.requests, "errors": client.errors[:10], "latency_s": round(time.monotonic() - t0, 2)}
