"""Turn ERC-20 Transfer records into economic trades: one trade per wallet per transaction.

Input records: {tx, block, from, to, value} (lowercase addresses, int value).
Routing hops (router -> settler -> pool) collapse because every address's delta is netted per tx:
an intermediary that merely forwards tokens nets to zero and is never counted.
Transfer events only exist for successful txs, so failed swaps never appear here.

Correctness rules (each one bought with a real false positive):
- Pools are netted PER POOL, never summed: routing between two pools (+100 / -100) must not cancel to
  "no pool activity" and hide a sale.
- A wallet is a seller/buyer only if its tokens have an EDGE PATH to/from a pool in that tx. Pool activity is
  never applied tx-globally: a multicall bundling "Alice -> friend" with "Bob -> pool" must not call Alice a seller.
- Only the zero address mints. 0xdead receives burns; it never has mint semantics.
- Burns are attributed by direct wallet->dead edges first, then via burn intermediaries (wallet -> burner -> dead,
  where the burner nets to zero), then exact-amount matching, then a single unexplained sender. Proportional
  splitting across unrelated wallets is never done when a pool is also involved (it turns sellers into burners
  and vice versa).
- Direct transfer edges are preserved (`counterparties`) so a dust input from a second wallet cannot
  erase a dev -> seller attribution link by collapsing the counterparty to "2 senders".
"""
from collections import defaultdict

ZERO = "0x0000000000000000000000000000000000000000"


def detect_pools(records, code_lookup, known=(), min_txs=3, max_cands=40, swap_check=None):
    """Pools = contracts that both RECEIVE tokens (net, in >= min_txs txs) and SEND tokens (net, in >= min_txs txs):
    a venue trades both ways. One-directional sinks (staking, lockers, fee splitters, distributors, burners) are NOT
    pools, so tokens parked there are 'transfer to contract', never 'sold'. Only repeat counterparties are classified
    (code_lookup(addrs) -> {addr: eoa|contract|eoa7702|dead}): a 6,000-transfer window costs one or two eth_getCode
    batches. Known pools always included. swap_check(addrs) -> set of addrs that emit swap events: lets a fresh,
    sell-only market (one-way flow in this window) still be recognised as a venue. Returns (pools, hits, kinds);
    hits[a] = (txs_received, txs_sent)."""
    per_tx = defaultdict(lambda: defaultdict(int))
    for r in records:
        per_tx[r["tx"]][r["from"]] -= r["value"]
        per_tx[r["tx"]][r["to"]] += r["value"]
    hits = defaultdict(lambda: [0, 0])
    for net in per_tx.values():
        for a, v in net.items():
            if v:
                hits[a][0 if v > 0 else 1] += 1
    known_lc = {a.lower() for a in known}
    cands = sorted((a for a, (i, o) in hits.items() if i >= min_txs and o >= min_txs), key=lambda a: -sum(hits[a]))[:max_cands]
    one_way = sorted((a for a, (i, o) in hits.items() if a not in cands and a not in known_lc
                      and max(i, o) >= min_txs and min(i, o) == 0),
                     key=lambda a: -sum(hits[a]))[:max_cands]
    kinds = code_lookup(cands + one_way) if (cands or one_way) else {}
    pools = {a.lower() for a in known}
    pools |= {a for a in cands if kinds.get(a) == "contract"}
    if swap_check:
        # one-way contracts are venues only with independent swap-event evidence, never on flow shape alone
        ow_contracts = [a for a in one_way if kinds.get(a) == "contract"]
        if ow_contracts:
            pools |= set(swap_check(ow_contracts)) & set(ow_contracts)
    tracked = set(cands) | set(one_way) | ({a.lower() for a in known} & set(hits))
    return pools, {a: tuple(hits[a]) for a in tracked}, kinds


def _reach(evs, seeds, backward):
    """Addresses with an edge path to (backward=True) or from (backward=False) any seed, within one tx's edges."""
    reach = set(seeds)
    changed = bool(reach)
    while changed:
        changed = False
        for e in evs:
            if backward:
                if e["to"] in reach and e["from"] not in reach:
                    reach.add(e["from"])
                    changed = True
            elif e["from"] in reach and e["to"] not in reach:
                reach.add(e["to"])
                changed = True
    return reach


def _direct(edges, addr, out):
    """Direct counterparty edges of one wallet in one tx: [(counterparty, value)], largest first."""
    agg = defaultdict(int)
    for e in edges:
        if out and e["from"] == addr:
            agg[e["to"]] += e["value"]
        elif not out and e["to"] == addr:
            agg[e["from"]] += e["value"]
    return sorted(agg.items(), key=lambda kv: -kv[1])


def trades(records, pools, dead):
    """-> [{tx, block, addr, kind, amount, counterparty[, counterparties]}], kind: sell|buy|burn|transfer_out|transfer_in.
    `counterparties` (direct edge counterparties, largest first) is added only when there is more than one."""
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
        # Mints (from the ZERO ADDRESS only) are not trades: a launch tx mints supply into the bonding-curve market
        # and then the launcher buys from it in the same tx. Remove minted amounts from that pool's delta so the buy
        # is seen as a buy. 0xdead is a burn destination, never a mint source.
        minted_to = defaultdict(int)
        for e in evs:
            if e["from"] == ZERO:
                minted_to[e["to"]] += e["value"]
        minted_to_wallet = {a for a in minted_to if a not in pools}
        # Per-pool deltas (mint-adjusted). Summing pools together lets two-pool routing cancel and hide a sale.
        pdelta = {a: v - minted_to.get(a, 0) for a, v in net.items() if a in pools}
        pool_gain = sum(v for v in pdelta.values() if v > 0)
        pool_loss = sum(-v for v in pdelta.values() if v < 0)
        # Pool involvement is edge-scoped, not tx-global: a wallet sells only if its tokens have an edge path
        # to a gaining pool (directly or via routers), and buys only if reachable from a losing pool.
        sell_reach = _reach(evs, {a for a, v in pdelta.items() if v > 0}, backward=True)
        buy_reach = _reach(evs, {a for a, v in pdelta.items() if v < 0}, backward=False)
        burned_total = sum(v for a, v in net.items() if a in dead and v > 0)
        wallets = {a: v for a, v in net.items() if a not in pools and a not in dead and v != 0}

        # --- burn attribution: direct edges + routed via zero-netting burn intermediaries, then exact-amount
        # match, then a single unexplained sender ---
        burn_of = defaultdict(int)
        if burned_total:
            # a burn intermediary nets to zero and forwards to a dead address; credit routed burns through it
            # up to what it actually forwarded, so a mixed-destination forwarder cannot over-credit
            inter_cap = {a: sum(e["value"] for e in evs if e["from"] == a and e["to"] in dead)
                         for a, v in net.items() if v == 0 and a not in pools and a != ZERO}
            inter_cap = {a: c for a, c in inter_cap.items() if c}
            for a, v in wallets.items():
                if v < 0:
                    direct_burn = sum(e["value"] for e in evs if e["from"] == a and e["to"] in dead)
                    routed = 0
                    for e in evs:
                        if e["from"] == a and inter_cap.get(e["to"], 0) > 0:
                            take = min(e["value"], inter_cap[e["to"]])
                            inter_cap[e["to"]] -= take
                            routed += take
                    burn_of[a] = min(-v, direct_burn + routed)
            rest = burned_total - sum(burn_of.values())
            if rest > 0:
                unexplained = [(a, -v - burn_of[a]) for a, v in wallets.items() if v < 0 and -v - burn_of[a] > 0]
                exact = [a for a, u in unexplained if u == rest]
                if len(exact) == 1:
                    burn_of[exact[0]] += rest
                elif len(unexplained) == 1:
                    a, u = unexplained[0]
                    burn_of[a] += min(u, rest)
                elif unexplained and pool_gain <= 0:
                    # pure burn tx (no venue involved): proportional split cannot mislabel a seller
                    tot = sum(u for _, u in unexplained)
                    for a, u in unexplained:
                        burn_of[a] += rest * u // tot
                # ambiguous burn+sell mix: leave the remainder unattributed rather than guess

        for a, v in sorted(wallets.items()):
            if v < 0:
                outflow = -v
                b = min(outflow, burn_of.get(a, 0))
                if b:
                    out.append(dict(tx=h, block=block, addr=a, kind="burn", amount=b, counterparty="dead"))
                remaining = outflow - b
                if not remaining:
                    continue
                if pool_gain > 0 and a in sell_reach:
                    out.append(dict(tx=h, block=block, addr=a, kind="sell", amount=remaining, counterparty="pool"))
                else:
                    cps = [(c, x) for c, x in _direct(evs, a, out=True) if c not in dead]
                    rec = dict(tx=h, block=block, addr=a, kind="transfer_out", amount=remaining,
                               counterparty=cps[0][0] if cps else "?")
                    if len(cps) > 1:
                        rec["counterparties"] = [c for c, _ in cps]
                    out.append(rec)
            else:
                if pool_loss > 0 and a in buy_reach:
                    out.append(dict(tx=h, block=block, addr=a, kind="buy", amount=v, counterparty="pool"))
                else:
                    cps = [(c, x) for c, x in _direct(evs, a, out=False) if c != ZERO and c not in pools]
                    cp = cps[0][0] if cps else ("mint" if a in minted_to_wallet else "?")
                    rec = dict(tx=h, block=block, addr=a, kind="transfer_in", amount=v, counterparty=cp)
                    if len(cps) > 1:
                        rec["counterparties"] = [c for c, _ in cps]
                    out.append(rec)
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
