"""Launchpad awareness, RPC-first. A launchpad's launch event gives token, market (bonding-curve) contract and launcher
in one eth_getLogs call, so the explorer is not needed to identify the developer wallet."""
from . import onchain

LOG_LIMIT_MSG = "exceeds limit"


def pads(client):
    return client.chain.get("launchpads") or []


def find(client, token, head_block):
    """-> {launcher, market, creation_tx, creation_block, launchpad} or None. Searches each configured launchpad."""
    for pad in pads(client):
        topics = [pad["topic"]] + [None] * 3
        topics[pad["token_topic"]] = onchain.pad(token)
        lo = max(0, head_block - onchain.MAX_RANGE)
        (res,) = client.rpc([("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(head_block), "address": pad["address"], "topics": topics}])])
        if res:
            lg = res[0]
            tp = lg["topics"]
            return {"launcher": onchain.topic_addr(tp[pad["launcher_topic"]]), "market": onchain.topic_addr(tp[pad["market_topic"]]),
                    "creation_tx": lg["transactionHash"], "creation_block": onchain._int(lg["blockNumber"]), "launchpad": pad["name"],
                    "initial_recipients": []}
    return None


def enumerate(client, pad, from_block, to_block, chunk=400_000):
    """All launches of a launchpad in a block range. Halves the chunk when the node's 10k-log limit is hit."""
    out = []
    start = from_block
    while start <= to_block:
        end = min(to_block, start + chunk)
        (res,) = client.rpc([("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end), "address": pad["address"], "topics": [pad["topic"]]}])])
        if res is None:
            if client.errors and LOG_LIMIT_MSG in client.errors[-1] and chunk > 1000:
                chunk //= 2
                continue
            raise RuntimeError(f"getLogs failed at {start}-{end}: {client.errors[-1] if client.errors else '?'}")
        for lg in res:
            tp = lg["topics"]
            out.append({"token": onchain.topic_addr(tp[pad["token_topic"]]), "market": onchain.topic_addr(tp[pad["market_topic"]]),
                        "launcher": onchain.topic_addr(tp[pad["launcher_topic"]]), "creation_tx": lg["transactionHash"],
                        "creation_block": onchain._int(lg["blockNumber"])})
        start = end + 1
    return out
