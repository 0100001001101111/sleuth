"""Launch-dump check, RPC-first: did the first buyers at launch dump within minutes, and did it run through one hub?

Pattern seen on Robinhood Chain Pons launches (DMN 2026-10-17, UMBRA 2026-10-18, reconstructed by hand before this
command existed): a handful of wallets buy a large share in the first trading block, forward it to one helper
contract, and that contract sells it back into the curve through dozens of fresh wallets within seconds. Everything
here comes from Transfer logs, eth_getCode and transaction senders; nothing is inferred from prices or names.

Three-valued verdict: True = first-block buyers took >= 5% of supply and >= 50% of that was sold inside the window
(directly or through a hub); False = checked with complete logs and the pattern is absent; None = incomplete evidence.
"""
from collections import defaultdict
from . import onchain, flows, launch
from .labels import DEAD

ZERO = "0x0000000000000000000000000000000000000000"

MIN_SHARE = 0.05
MIN_DUMPED = 0.5
BUNDLE_MIN_WALLETS = 3   # a bundled launch: several wallets take a large slice in the first blocks (sold later or not)
BUNDLE_MIN_SHARE = 0.25


def analyze(records, market, launcher, creation_block, supply, codes, senders, first_blocks=3, pools=(), end_block=None, dead=DEAD):
    """Pure function. records: Transfer records (lowercase, int value). codes(addrs)->{addr: eoa|contract|...};
    senders(tx_hashes)->{tx: from_address}. Returns the structured result (no I/O)."""
    pools = {flows.norm(p) for p in pools if p} | ({flows.norm(market)} if market else set())
    launcher = flows.norm(launcher) if launcher else None
    tr = flows.trades(records, pools, dead)
    lo, hi = creation_block, creation_block + first_blocks
    early = defaultdict(int)
    for t in tr:
        if t["kind"] == "buy" and lo <= t["block"] < hi:
            early[t["addr"]] += t["amount"]
    early_total = sum(early.values())
    # Forwarding: tokens the early buyers moved to a non-pool address inside the window.
    fwd, fwd_from = defaultdict(int), defaultdict(set)
    for r in records:
        if r["from"] in early and r["to"] not in pools and r["to"] not in dead and r["to"] not in early:
            fwd[r["to"]] += r["value"]
            fwd_from[r["to"]].add(r["from"])
    hub_cands = [a for a in fwd if len(fwd_from[a]) >= 2 or (early_total and fwd[a] >= 0.5 * early_total)]
    kinds = codes(hub_cands + list(early)) if (hub_cands or early) else {}
    hubs = {a for a in hub_cands if kinds.get(a) == "contract"}
    # A hub may hold the tokens and sell later (its own net sell), or pull from the buyer and sell inside ONE tx
    # (the buyer nets as the seller while the hub nets to zero). Both count as "via hub": look inside each sell's tx.
    hub_in_tx = defaultdict(set)
    for r in records:
        if r["to"] in hubs or r["from"] in hubs:
            hub_in_tx[r["tx"]].add(r["to"] if r["to"] in hubs else r["from"])
    sold_direct = sum(t["amount"] for t in tr if t["kind"] == "sell" and t["addr"] in early and t["tx"] not in hub_in_tx)
    hub_sells = [t for t in tr if t["kind"] == "sell" and (t["addr"] in hubs or (t["addr"] in early and t["tx"] in hub_in_tx))]
    sold_via_hub = sum(t["amount"] for t in hub_sells)
    hub_tx = list(dict.fromkeys(t["tx"] for t in hub_sells))
    hub_senders = senders(hub_tx) if hub_tx else {}
    dumped = sold_direct + sold_via_hub
    sell_blocks = [t["block"] for t in tr if t["kind"] == "sell" and (t["addr"] in early or t["addr"] in hubs)]
    first_sell = min(sell_blocks) if sell_blocks else None
    share_bought = (early_total / supply) if supply else None
    share_dumped = (dumped / supply) if supply else None
    dumped_fraction = (dumped / early_total) if early_total else None
    complete = bool(records) and supply is not None
    if not complete:
        verdict = None
    else:
        verdict = bool(share_bought >= MIN_SHARE and dumped_fraction is not None and dumped_fraction >= MIN_DUMPED)
    non_launcher = {a: v for a, v in early.items() if a != launcher and kinds.get(a, "eoa") != "contract"}
    bundle_share = (sum(non_launcher.values()) / supply) if supply else None
    bundled = bool(complete and len(non_launcher) >= BUNDLE_MIN_WALLETS and bundle_share is not None and bundle_share >= BUNDLE_MIN_SHARE)
    return {
        "bundled": bundled if complete else None, "bundle_wallets": len(non_launcher), "bundle_share": bundle_share,
        "creation_block": creation_block, "first_blocks": first_blocks, "end_block": end_block,
        "market": market, "launcher": launcher, "supply": supply,
        "early_buyers": [{"addr": a, "amount": v, "pct_supply": round(100 * v / supply, 3) if supply else None,
                          "kind": kinds.get(a, "unknown"), "is_launcher": a == launcher}
                         for a, v in sorted(early.items(), key=lambda kv: -kv[1])],
        "early_total": early_total, "share_bought": share_bought,
        "hubs": [{"addr": a, "received": fwd[a], "from_n": len(fwd_from[a]), "kind": kinds.get(a, "unknown")} for a in sorted(hubs)],
        "forwarded_to_non_hub": {a: v for a, v in fwd.items() if a not in hubs},
        "sold_direct": sold_direct, "sold_via_hub": sold_via_hub, "dumped": dumped,
        "share_dumped": share_dumped, "dumped_fraction": dumped_fraction,
        "hub_sell_txs": len(hub_tx), "hub_sellers": len(set(hub_senders.values())) if hub_senders else 0,
        "first_sell_block": first_sell, "blocks_to_first_sell": (first_sell - creation_block) if first_sell is not None else None,
        "coverage": {"transfers": len(records), "complete": complete},
        "launch_dump": verdict,
    }


def tx_senders(client, tx_hashes):
    """{tx: from} via batched eth_getTransactionByHash (immutable; cached when a cache is attached)."""
    out, need = {}, []
    for h in dict.fromkeys(tx_hashes):
        v, _ = client.cache.get(f"txfrom:{client.chain_id}:{h}") if client.cache else (None, None)
        if v:
            out[h] = v
        else:
            need.append(h)
    if need:
        res = client.rpc([("eth_getTransactionByHash", [h]) for h in need])
        for h, tx in zip(need, res):
            if isinstance(tx, dict) and tx.get("from"):
                out[h] = tx["from"].lower()
                if client.cache:
                    client.cache.put(f"txfrom:{client.chain_id}:{h}", out[h], client.chain_id)
            else:
                client.errors.append(f"rpc tx missing: {h}")
    return out


def run(client, token, blocks=3000, first_blocks=3, from_block=None):
    token = token.lower()
    head = client.head()
    out = {"stage": "launchdump", "chain_id": client.chain_id, "head": head, "token": token, "gaps": []}
    tok = onchain.erc20_meta(client, token) or {}
    supply = tok.get("total_supply")
    out["token_meta"] = tok
    li = launch.find(client, token, head["block"]) if from_block is None else None
    if li:
        creation_block, market, launcher = li["creation_block"], li["market"], li["launcher"]
        out["launch"] = li
    elif from_block is not None:
        creation_block, market, launcher = from_block, None, None
        out["gaps"].append("no launchpad event used: market unknown, pools come from config/auto-detection only")
    else:
        # Fallback for tokens launched outside a configured launchpad: the first mint (Transfer from 0x0) marks the
        # launch, and its recipient is the market if it is a contract (a bonding curve or pool seeded at creation).
        mints = onchain.transfer_logs(client, token, max(0, head["block"] - onchain.MAX_RANGE), head["block"], frm=ZERO)
        if not mints:
            out["gaps"].append("no launchpad event and no mint found in the last 10M blocks: pass --from-block (creation block)")
            out["summary"] = "cannot locate the launch; pass --from-block"
            return out
        first = min(mints, key=lambda r: (r["block"], r.get("idx", 0)))
        creation_block, launcher = first["block"], None
        kinds = onchain.codes(client, [first["to"]])
        market = first["to"] if kinds.get(first["to"]) == "contract" else None
        out["launch"] = {"creation_block": creation_block, "creation_tx": first["tx"], "mint_to": first["to"], "market": market,
                         "launcher": None, "launchpad": None, "via": "first mint"}
        out["gaps"].append("launch located from the first mint, not a launchpad event: launcher unknown" + ("" if market else "; market unknown"))
    end_block = min(head["block"], creation_block + blocks)
    records = onchain.transfer_logs(client, token, creation_block, end_block)
    known_pools = {p.lower() for p in client.chain.get("known_pools", [])}
    pools, _, _ = flows.detect_pools(records, lambda ad: onchain.codes(client, ad), known_pools | ({market.lower()} if market else set()))
    if supply is None and records:
        supply = sum(r["value"] for r in records if r["from"] in DEAD) or None  # minted in the window
        if supply:
            out["gaps"].append("totalSupply unavailable from RPC; supply = amount minted inside the window")
    res = analyze(records, market, launcher, creation_block, supply, lambda ad: onchain.codes(client, ad),
                  lambda hs: tx_senders(client, hs), first_blocks, pools, end_block)
    times, approx = onchain.block_times(client, [b for b in (creation_block, res["first_sell_block"], end_block) if b is not None])
    res["times"] = {"creation": times.get(creation_block), "first_sell": times.get(res["first_sell_block"]) if res["first_sell_block"] else None,
                    "end": times.get(end_block), "approx": approx}
    out.update(res)
    return out
