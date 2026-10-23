"""Decode one transaction, RPC-first: signer, target, status, every ERC-20 Transfer edge across ALL tokens in the
receipt, per-(token, address) nets, swap-event evidence, and contract/EOA labels. Answers "what IS this tx?" without
trusting any single venue heuristic. Explorer internal-ETH data is best-effort and marked partial when missing."""
from . import onchain
from .labels import DEAD

META_CAP = 6     # erc20_meta eth_calls per decode
CODES_CAP = 25   # eth_getCode lookups per decode


def decode(client, tx_hash):
    rc, tx = client.rpc([("eth_getTransactionReceipt", [tx_hash]), ("eth_getTransactionByHash", [tx_hash])])
    if not isinstance(rc, dict):
        return {"tx": tx_hash, "found": False, "gaps": ["receipt not found (bad hash, wrong chain, or RPC failure)"]}
    block = onchain._int(rc.get("blockNumber"))
    out = {"tx": tx_hash, "found": True, "block": block, "ts": onchain.block_time(client, block),
           "status": "ok" if rc.get("status") == "0x1" else "failed",
           "signer": (rc.get("from") or "").lower(), "to": (rc.get("to") or "").lower() or None,
           "eth_value": round(onchain._int((tx or {}).get("value")) / 1e18, 6) if tx else None,
           "gaps": []}

    swap_topics = set(client.chain.get("swap_topics") or [])
    known_pools = {p.lower() for p in client.chain.get("known_pools", [])}
    services = client.cfg.get("known_services", {})

    edges, swap_logs, topic0s = [], [], set()
    for lg in rc.get("logs") or []:
        tp = lg.get("topics") or []
        if not tp:
            continue
        topic0s.add(tp[0])
        emitter = (lg.get("address") or "").lower()
        if tp[0] in swap_topics:
            swap_logs.append(emitter)
        if tp[0] == onchain.TRANSFER and len(tp) == 3:
            edges.append({"token": emitter, "from": onchain.topic_addr(tp[1]), "to": onchain.topic_addr(tp[2]),
                          "value": onchain._int(lg.get("data"))})
    out["swap_evidence"] = {"found": bool(swap_logs), "emitting_contracts": sorted(set(swap_logs))}

    tokens = list(dict.fromkeys(e["token"] for e in edges))
    metas = {}
    for tk in tokens[:META_CAP]:
        metas[tk] = onchain.erc20_meta(client, tk)
    if len(tokens) > META_CAP:
        out["gaps"].append(f"{len(tokens)} tokens moved; metadata fetched for the first {META_CAP} only")

    nets = {}
    for e in edges:
        nets.setdefault(e["token"], {})
        nets[e["token"]][e["from"]] = nets[e["token"]].get(e["from"], 0) - e["value"]
        nets[e["token"]][e["to"]] = nets[e["token"]].get(e["to"], 0) + e["value"]

    addrs = list(dict.fromkeys([out["signer"]] + ([out["to"]] if out["to"] else []) +
                               [a for n in nets.values() for a in n] + sorted(set(swap_logs))))
    kinds = onchain.codes(client, addrs[:CODES_CAP])
    if len(addrs) > CODES_CAP:
        out["gaps"].append(f"{len(addrs)} addresses involved; code-checked the first {CODES_CAP} only")

    def label(a):
        if a == "0x0000000000000000000000000000000000000000":
            return "zero (mint/burn)"
        if a in DEAD:
            return "dead"
        if a in services:
            return services[a]
        if a in known_pools:
            return "known pool"
        return kinds.get(a, "unknown")

    def fmt_amt(tk, v):
        m = metas.get(tk)
        return round(v / 10 ** m["decimals"], 6) if m else v

    out["tokens"] = {tk: {"symbol": (metas.get(tk) or {}).get("symbol"), "decimals": (metas.get(tk) or {}).get("decimals")}
                     for tk in tokens}
    out["transfers"] = [{"token": e["token"], "symbol": (metas.get(e["token"]) or {}).get("symbol"),
                         "from": e["from"], "from_label": label(e["from"]),
                         "to": e["to"], "to_label": label(e["to"]), "amount": fmt_amt(e["token"], e["value"])}
                        for e in edges]
    out["nets"] = {tk: {a: fmt_amt(tk, v) for a, v in sorted(n.items(), key=lambda kv: kv[1]) if v}
                   for tk, n in nets.items()}
    out["labels"] = {a: label(a) for a in addrs[:CODES_CAP]}

    # Internal ETH movements: explorer-only, slow, may be down -- a miss is a marked gap, never silence.
    internal = client.get(f"/transactions/{tx_hash}/internal-transactions", soft=True)
    if internal is None:
        out["internal_eth"] = None
        out["gaps"].append("explorer unreachable: internal ETH movements not decoded")
    else:
        out["internal_eth"] = [{"from": ((x.get("from") or {}).get("hash") or "").lower(),
                                "to": ((x.get("to") or {}).get("hash") or "").lower(),
                                "eth": round(int(x.get("value") or 0) / 1e18, 6)}
                               for x in (internal.get("items") or []) if int(x.get("value") or 0) > 0][:20]
    return out
