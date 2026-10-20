"""Burn verification, RPC-first. Reads the receipt's Transfer logs, so burns executed through internal /
delegated (EIP-7702) calls are found even when the tx's own `to` is not the token.
Distinguishes transfer-to-dead (totalSupply unchanged) from supply-reducing burns."""
from . import onchain
from .labels import DEAD


def check_tx(client, tx_hash, token=None):
    (rc,) = client.rpc([("eth_getTransactionReceipt", [tx_hash])])
    if not rc:
        return {"tx": tx_hash, "found": False}
    sender = (rc.get("from") or "").lower()
    to = (rc.get("to") or "").lower()
    block = onchain._int(rc.get("blockNumber"))
    out = {"tx": tx_hash, "found": True, "status": "ok" if rc.get("status") == "0x1" else "failed", "block": block,
           "block_hash": rc.get("blockHash"), "ts": onchain.block_time(client, block), "sender": sender, "to": to, "burns": []}
    kinds = onchain.codes(client, [sender])
    out["sender_delegated_eip7702"] = kinds.get(sender) == "eoa7702"
    for lg in rc.get("logs") or []:
        tp = lg.get("topics") or []
        if len(tp) != 3 or tp[0] != onchain.TRANSFER:
            continue
        ta = (lg.get("address") or "").lower()
        if token and ta != token.lower():
            continue
        dst = onchain.topic_addr(tp[2])
        if dst in DEAD:
            meta = onchain.erc20_meta(client, ta) or {}
            out["burns"].append({"token": ta, "symbol": meta.get("symbol"), "from": onchain.topic_addr(tp[1]), "to": dst,
                                 "amount": onchain._int(lg.get("data")), "decimals": meta.get("decimals", 18)})
    out["executed_via_internal_call"] = bool(out["burns"]) and to not in {b["token"] for b in out["burns"]}
    if out["burns"]:
        tk = out["burns"][0]["token"]
        before, after = client.total_supply(tk, block - 1), client.total_supply(tk, block)
        out["total_supply_before"], out["total_supply_after"] = before, after
        if before is not None and after is not None:
            out["supply_reduced"] = after < before
            out["burn_type"] = ("supply-reducing burn (block-level totalSupply comparison)" if after < before
                                else "transfer to dead address (supply unchanged, tokens unrecoverable)")
        else:
            # Non-archive RPC: no historical state. Fall back to ERC-20 convention: _burn emits Transfer(from, 0x0)
            # and reduces totalSupply; a transfer to 0x...dEaD leaves totalSupply unchanged.
            zero = all(b["to"] == "0x0000000000000000000000000000000000000000" for b in out["burns"])
            out["supply_reduced"] = zero
            out["burn_type"] = ("supply-reducing burn (inferred: Transfer to 0x0; historical totalSupply unavailable on this RPC)" if zero
                                else "transfer to dead address (supply unchanged, tokens unrecoverable; inferred from destination)")
            out["total_supply_now"] = client.total_supply(tk)
    tx = client.get(f"/transactions/{tx_hash}", soft=True) if client.remaining() is None or client.remaining() > 3 else None
    out["method"] = (tx or {}).get("method")
    return out


def check_token(client, token, window_s, head=None):
    """All dead-address transfers of a token in the window (RPC logs), with current totalSupply."""
    head = head or client.head()
    since = head["block_time"] - window_s
    lo = onchain.block_at_time(client, since, head["block"], head["block_time"])
    recs = []
    for d in DEAD:
        recs += onchain.transfer_logs(client, token, lo, head["block"], to=d)
    times, approx = onchain.block_times(client, [r["block"] for r in recs])
    burns = [{"tx": r["tx"], "ts": times.get(r["block"]), "block": r["block"], "from": r["from"], "amount": r["value"]} for r in recs]
    return {"token": token, "burns": burns, "from_block": lo, "to_block": head["block"], "complete": True, "timestamps_approx": approx,
            "total_supply_now": client.total_supply(token)}
