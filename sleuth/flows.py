"""Turn ERC-20 Transfer records into economic trades: one trade per wallet per transaction.

Input records: {tx, block, from, to, value} (lowercase addresses, int value).
Routing hops (router -> settler -> pool) collapse because every address's delta is netted per tx:
an intermediary that merely forwards tokens nets to zero and is never counted.
Transfer events only exist for successful txs, so failed swaps never appear here.
"""
from collections import defaultdict


def detect_pools(records, code_lookup, known=(), min_txs=3, max_cands=40):
    """Pools = contracts that both RECEIVE tokens (net, in >= min_txs txs) and SEND tokens (net, in >= min_txs txs):
    a venue trades both ways. One-directional sinks (staking, lockers, fee splitters, distributors, burners) are NOT
    pools, so tokens parked there are 'transfer to contract', never 'sold'. Only repeat counterparties are classified
    (code_lookup(addrs) -> {addr: eoa|contract|eoa7702|dead}): a 6,000-transfer window costs one or two eth_getCode
    batches. Known pools always included. Returns (pools, hits, kinds); hits[a] = (txs_received, txs_sent)."""
    per_tx = defaultdict(lambda: defaultdict(int))
    for r in records:
        per_tx[r["tx"]][r["from"]] -= r["value"]
        per_tx[r["tx"]][r["to"]] += r["value"]
    hits = defaultdict(lambda: [0, 0])
    for net in per_tx.values():
        for a, v in net.items():
            if v:
                hits[a][0 if v > 0 else 1] += 1
    cands = sorted((a for a, (i, o) in hits.items() if i >= min_txs and o >= min_txs), key=lambda a: -sum(hits[a]))[:max_cands]
    kinds = code_lookup(cands) if cands else {}
    pools = {a.lower() for a in known}
    pools |= {a for a in cands if kinds.get(a) == "contract"}
    return pools, {a: tuple(hits[a]) for a in cands}, kinds


def trades(records, pools, dead):
    """-> [{tx, block, addr, kind, amount, counterparty}], kind: sell|buy|burn|transfer_out|transfer_in."""
    by_tx = defaultdict(list)
    for r in records:
        by_tx[r["tx"]].append(r)
    out = []
    for h, evs in by_tx.items():
        net = defaultdict(int)
        for e in evs:
            net[e["from"]] -= e["value"]
            net[e["to"]] += e["value"]
        block = evs[0]["block"]
        # Mints (from 0x0) are not trades: a launch tx mints supply into the bonding-curve market and then the launcher
        # buys from it in the same tx. Remove minted amounts from the pool delta so that buy is seen as a buy.
        minted_to_pool = sum(e["value"] for e in evs if e["from"] in dead and e["to"] in pools)
        minted_to_wallet = {e["to"] for e in evs if e["from"] in dead and e["to"] not in pools}
        pool_delta = sum(v for a, v in net.items() if a in pools) - minted_to_pool
        burned = sum(v for a, v in net.items() if a in dead and v > 0)
        wallets = {a: v for a, v in net.items() if a not in pools and a not in dead and v != 0}
        if burned:
            # Split each sender's outflow: the dead-address share is a burn, anything the pool absorbed is a sale.
            total_out = sum(-v for v in wallets.values() if v < 0) or 1
            for a, v in wallets.items():
                if v < 0:
                    b = min(-v, burned * (-v) // total_out) if pool_delta > 0 else -v
                    if b:
                        out.append(dict(tx=h, block=block, addr=a, kind="burn", amount=b, counterparty="dead"))
                    if pool_delta > 0 and -v - b > 0:
                        out.append(dict(tx=h, block=block, addr=a, kind="sell", amount=-v - b, counterparty="pool"))
            continue
        if pool_delta > 0:
            for a, v in wallets.items():
                if v < 0:
                    out.append(dict(tx=h, block=block, addr=a, kind="sell", amount=-v, counterparty="pool"))
        elif pool_delta < 0:
            for a, v in wallets.items():
                if v > 0:
                    out.append(dict(tx=h, block=block, addr=a, kind="buy", amount=v, counterparty="pool"))
        else:
            senders = [(a, -v) for a, v in wallets.items() if v < 0]
            receivers = [(a, v) for a, v in wallets.items() if v > 0]
            for a, v in senders:
                cp = receivers[0][0] if len(receivers) == 1 else f"{len(receivers)} recipients"
                out.append(dict(tx=h, block=block, addr=a, kind="transfer_out", amount=v, counterparty=cp))
            for a, v in receivers:
                cp = senders[0][0] if len(senders) == 1 else ("mint" if not senders and a in minted_to_wallet else f"{len(senders)} senders")
                out.append(dict(tx=h, block=block, addr=a, kind="transfer_in", amount=v, counterparty=cp))
    return out


def aggregate(tr):
    """Per-address totals: sold, bought, burned, transferred_out, transferred_in, n, txs (first 3 sell txs)."""
    agg = defaultdict(lambda: dict(sold=0, bought=0, burned=0, transferred_out=0, transferred_in=0, n=0, txs=[]))
    key = {"sell": "sold", "buy": "bought", "burn": "burned", "transfer_out": "transferred_out", "transfer_in": "transferred_in"}
    for t in tr:
        a = agg[t["addr"]]
        a[key[t["kind"]]] += t["amount"]
        a["n"] += 1
        if t["kind"] == "sell" and len(a["txs"]) < 3:
            a["txs"].append(t["tx"])
    return agg
