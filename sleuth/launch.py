"""Launchpad awareness, RPC-first. A launchpad's launch event gives token, market (bonding-curve) contract and launcher
in one eth_getLogs call, so the explorer is not needed to identify the developer wallet."""
from . import onchain

LOG_LIMIT_MSG = "exceeds limit"


def pads(client):
    return client.chain.get("launchpads") or []


def find(client, token, head_block):
    """-> {launcher, launcher_source, market, creation_tx, creation_block, launchpad, initial_recipients} or None.
    Searches each configured launchpad's launch event, then falls back to the token's first mint (so a NEW, unconfigured
    launcher contract -- the GH0ST failure -- still yields a developer candidate instead of a hollow verdict)."""
    for pad in pads(client):
        topics = [pad["topic"]] + [None] * 3
        topics[pad["token_topic"]] = onchain.pad(token)
        lo = max(0, head_block - onchain.MAX_RANGE)
        (res,) = client.rpc([("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(head_block), "address": pad["address"], "topics": topics}])])
        if res:
            lg = res[0]
            tp = lg["topics"]
            return {"launcher": onchain.topic_addr(tp[pad["launcher_topic"]]), "launcher_source": "launch_event",
                    "market": onchain.topic_addr(tp[pad["market_topic"]]),
                    "creation_tx": lg["transactionHash"], "creation_block": onchain._int(lg["blockNumber"]), "launchpad": pad["name"],
                    "initial_recipients": []}
    return find_by_mint(client, token, head_block)


def find_by_mint(client, token, head_block):
    """Fallback launcher identification without the explorer: the token's first mint (Transfer from 0x0) and that
    tx's signer. The signer is a launcher CANDIDATE only -- it may be a relayer, smart account or launch service --
    so launcher_source says how the identity was derived and reports must not present it as proven. The scan walks
    from block 0 FORWARD so the first non-empty span truly holds the first mint; scanning only the newest span would
    mistake a later emission mint of an older inflationary token for its launch."""
    logs, lo = [], 0
    while lo <= head_block:
        hi = min(head_block, lo + onchain.MAX_RANGE)
        try:
            logs = onchain.transfer_logs(client, token, lo, hi,
                                         frm="0x0000000000000000000000000000000000000000")
        except onchain.RpcError:
            return None
        if logs:
            break
        lo = hi + 1
    if not logs:
        return None
    first_block = logs[0]["block"]
    firsts = [l for l in logs if l["block"] == first_block and l["tx"] == logs[0]["tx"]]
    ctx = firsts[0]["tx"]
    (tx,) = client.rpc([("eth_getTransactionByHash", [ctx])])
    signer = ((tx or {}).get("from") or "").lower()
    if not signer:
        return None
    recips = list(dict.fromkeys(l["to"] for l in firsts))
    kinds = onchain.codes(client, recips)
    market = next((a for a in recips if kinds.get(a) == "contract"), None)
    return {"launcher": signer, "launcher_source": "creation_tx_signer (candidate: may be a relayer or launch service)",
            "market": market, "market_source": "first contract mint recipient (candidate: may be a vault or locker)",
            "creation_tx": ctx, "creation_block": first_block, "launchpad": None,
            "initial_recipients": [{"addr": l["to"], "amount": l["value"]} for l in firsts
                                   if kinds.get(l["to"]) in ("eoa", "eoa7702")]}


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
