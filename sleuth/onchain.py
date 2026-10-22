"""RPC-first data access: Transfer logs, block times, code checks, balances, ERC-20 metadata.
Measured on Robinhood Chain RPC: ~0.1-0.6s per call vs 2-10s per Blockscout page. eth_getLogs allows <=10M blocks/query."""
import time
from .labels import DEAD

class RpcError(Exception):
    """An RPC call returned no result; callers must not treat that as 'no activity'."""


TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MAX_RANGE = 9_999_000
LOG_CAP = 10_000
_CAP_MARKERS = ("exceeds limit", "limit of 10000", "query returned more than", "response size exceed", "too many",
                "block range", "are allowed", "timed out")


def pad(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def topic_addr(t):
    return "0x" + t[-40:].lower()


def _int(h):
    return int(h, 16) if h and h != "0x" else 0


def transfer_logs(client, token, from_block, to_block, frm=None, to=None):
    """Normalized ERC-20 Transfer events: {tx, block, idx, from, to, value}, block-ordered. Only successful txs emit logs.
    Splits ranges recursively on the node's 10k-log cap; a result AT the cap is treated as possibly truncated and split
    too, because some providers truncate silently instead of erroring."""
    topics = [TRANSFER, pad(frm) if frm else None, pad(to) if to else None]
    while topics and topics[-1] is None:
        topics.pop()
    out = []
    spans = [(max(0, from_block), to_block)]
    while spans:
        start, end = spans.pop()
        if start > end:
            continue
        if end - start > MAX_RANGE:
            mid = (start + end) // 2
            spans += [(mid + 1, end), (start, mid)]
            continue
        n_err = len(client.errors)
        (res,) = client.rpc([("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end), "address": token, "topics": topics}])])
        if not isinstance(res, list):
            err = "; ".join(client.errors[n_err:]) or "no result"
            if end > start and any(m in err.lower() for m in _CAP_MARKERS):
                mid = (start + end) // 2
                spans += [(mid + 1, end), (start, mid)]
                continue
            raise RpcError(f"eth_getLogs {start}-{end} failed: {err}")
        if len(res) >= LOG_CAP:
            if end > start:
                mid = (start + end) // 2
                spans += [(mid + 1, end), (start, mid)]
                continue
            # a single block at the cap cannot be split: flag it so callers degrade coverage instead of
            # silently accepting a possibly-truncated page
            client.errors.append(f"rpc eth_getLogs: block {start} returned {LOG_CAP} logs (at cap, possibly truncated)")
        for lg in res:
            tp = lg.get("topics") or []
            if len(tp) < 3:
                continue
            out.append({"tx": lg["transactionHash"], "block": _int(lg["blockNumber"]), "idx": _int(lg.get("logIndex")),
                        "from": topic_addr(tp[1]), "to": topic_addr(tp[2]), "value": _int(lg.get("data"))})
    out.sort(key=lambda x: (x["block"], x["idx"]))
    return out


def swap_evidence(client, addrs, spans, topics, chunk=99_000):
    """Which addrs emitted any of the given swap topic0s near their token-activity span (spans: addr -> (min_block,
    max_block)). At most 2 getLogs per addr, each <= chunk blocks: this node caps topic-filtered queries at 100k blocks.
    Returns (confirmed, unresolved). Fail-closed: an RPC failure, or no configured topics, lands the addr in
    `unresolved` -- never in a silent 'no evidence', which would let a sale at that venue read as a plain transfer."""
    addrs = [a for a in addrs if spans.get(a)]
    if not topics:
        return set(), set(addrs)

    def _pass(check, width):
        calls, who = [], []
        for a in check:
            mn, mx = spans[a]
            wins = [(mn, min(mx, mn + width))]
            if mx > mn + width:
                wins.append((max(mn, mx - width), mx))  # swaps co-occur with token flow: first + last activity chunks
            for s, e in wins:
                calls.append(("eth_getLogs", [{"fromBlock": hex(s), "toBlock": hex(e), "address": a, "topics": [topics]}]))
                who.append(a)
        res = client.rpc(calls)
        conf, unres = set(), set()
        for a, r in zip(who, res):
            if isinstance(r, list):
                if r:
                    conf.add(a)
            else:
                unres.add(a)
        # Two windows SAMPLE a span wider than 2*width -- clean empty samples are not proof of 'no swaps ever'
        # (a swap in the omitted middle would make this a false sink), so an uncovered, unconfirmed addr stays unresolved.
        sampled = {a for a in check if spans[a][1] - spans[a][0] > 2 * width}
        return conf, (unres | sampled) - conf

    confirmed, unresolved = _pass(addrs, chunk)
    if unresolved:
        # A busy venue FAILS the wide query (>10k matching logs, or timeout) -- the failure itself suggests heavy
        # activity, so retry with tiny windows; the retry can only CONFIRM, never clear an addr to 'sink'.
        conf2, _ = _pass(sorted(unresolved), 1_500)
        confirmed |= conf2
        unresolved -= conf2
    return confirmed, unresolved


def block_time(client, block):
    (b,) = client.rpc([("eth_getBlockByNumber", [hex(block) if isinstance(block, int) else block, False])])
    return _int(b["timestamp"]) if b else None


def block_times(client, blocks, exact_limit=40, anchors_n=20):
    """block -> unix ts. Exact when <= exact_limit distinct blocks, else anchors_n anchors + linear interpolation (approx=True)."""
    bl = sorted(set(b for b in blocks if b is not None))
    if not bl:
        return {}, False
    if len(bl) <= exact_limit:
        res = client.rpc([("eth_getBlockByNumber", [hex(b), False]) for b in bl])
        return {b: (_int(r["timestamp"]) if isinstance(r, dict) and r.get("timestamp") else None) for b, r in zip(bl, res)}, False
    step = max(1, (len(bl) - 1) // (anchors_n - 1))
    anchors = sorted(set(bl[::step] + [bl[-1]]))
    res = client.rpc([("eth_getBlockByNumber", [hex(b), False]) for b in anchors])
    known = [(b, _int(r["timestamp"])) for b, r in zip(anchors, res) if isinstance(r, dict) and r.get("timestamp")]
    if not known:
        return {b: None for b in bl}, True
    out = {}
    j = 0
    for b in bl:
        while j + 1 < len(known) - 1 and known[j + 1][0] <= b:
            j += 1
        (b0, t0), (b1, t1) = known[j], known[min(j + 1, len(known) - 1)]
        out[b] = t0 if b1 == b0 else round(t0 + (t1 - t0) * (b - b0) / (b1 - b0))
    return out, True


def block_at_time(client, ts, head_block, head_ts, iters=4):
    """Approximate largest block with timestamp <= ts: secant search to within ~50 blocks (~5 s on this chain) using 3-5
    RPC calls; beyond 200k blocks back it extrapolates from the observed rate. Callers report the window as block numbers."""
    lo_b, lo_t = max(0, head_block - 200_000), None
    lo_t = block_time(client, lo_b)
    if lo_t is None:
        return max(0, head_block - int((head_ts - ts) / 0.104))
    if ts <= lo_t:  # beyond 200k blocks back: extrapolate by rate
        rate = (head_block - lo_b) / max(1, head_ts - lo_t)
        return max(0, int(lo_b - (lo_t - ts) * rate))
    hi_b, hi_t = head_block, head_ts
    guess = lo_b
    for _ in range(iters):
        if hi_t == lo_t:
            break
        guess = int(lo_b + (hi_b - lo_b) * (ts - lo_t) / (hi_t - lo_t))
        guess = min(max(guess, lo_b + 1), hi_b - 1)
        gt = block_time(client, guess)
        if gt is None:
            break
        if gt <= ts:
            lo_b, lo_t = guess, gt
        else:
            hi_b, hi_t = guess, gt
        if hi_b - lo_b <= 50:
            break
    return lo_b


EOA_CACHE_S = 6 * 3600


def codes(client, addrs):
    """addr -> 'eoa' | 'contract' | 'eoa7702'. Contracts cached forever (durable); EOA answers cached 6h."""
    addrs = [a.lower() for a in dict.fromkeys(addrs)]
    out, need = {}, []
    for a in addrs:
        if a in DEAD:
            out[a] = "dead"
            continue
        v, meta = client.cache.get(f"code:{client.chain_id}:{a}") if client.cache else (None, None)
        if v == "contract" or (v and time.time() - (meta or {}).get("fetched_at", 0) < EOA_CACHE_S):
            out[a] = v
        else:
            need.append(a)
    res = client.rpc([("eth_getCode", [a, "latest"]) for a in need])
    for a, c in zip(need, res):
        if c is None:
            out[a] = "unknown"
        else:
            out[a] = "eoa" if c in ("0x", "") else ("eoa7702" if c.lower().startswith("0xef0100") else "contract")
            if client.cache:
                client.cache.put(f"code:{client.chain_id}:{a}", out[a], client.chain_id)
    return out


def balances(client, token, addrs):
    res = client.rpc([("eth_call", [{"to": token, "data": "0x70a08231" + pad(a)[2:]}, "latest"]) for a in addrs])
    return {a.lower(): (_int(r) if r is not None else None) for a, r in zip(addrs, res)}


def _str(h):
    if not h or h == "0x":
        return None
    raw = bytes.fromhex(h[2:])
    if len(raw) >= 64:
        off = int.from_bytes(raw[:32], "big")
        ln = int.from_bytes(raw[off:off + 32], "big")
        return raw[off + 32:off + 32 + ln].decode("utf-8", "replace")
    return raw.rstrip(b"\x00").decode("utf-8", "replace")


def erc20_meta(client, token):
    """symbol/name/decimals/totalSupply via eth_call. Identity cached; totalSupply always live. None if not ERC-20."""
    key = f"erc20:{client.chain_id}:{token.lower()}"
    v, _ = client.cache.get(key) if client.cache else (None, None)
    calls = [("eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"])]
    if not v:
        calls += [("eth_call", [{"to": token, "data": s}, "latest"]) for s in ("0x95d89b41", "0x06fdde03", "0x313ce567")]
    res = client.rpc(calls)
    if not v:
        if res[3] is None or res[3] == "0x":
            return None
        v = {"symbol": _str(res[1]), "name": _str(res[2]), "decimals": _int(res[3])}
        if client.cache:
            client.cache.put(key, v, client.chain_id)
    v = dict(v)
    v["total_supply"] = _int(res[0]) if res[0] is not None else None
    v["address"] = token
    return v


def tx_records_full(client, tx_hashes, token):
    """(records, topics) via batched eth_getTransactionReceipt: `records` are `token`'s normalized Transfer records
    (lets routed hops wallet -> router -> pool be netted per tx); `topics` maps tx -> {topic0: sorted emitter
    addresses}, so callers can check swap-event evidence AND which contract emitted it (a swap of an unrelated token
    elsewhere in a multicall must not confirm this token's outflow as a sale). Receipts are immutable: cached."""
    token = token.lower()
    hs = list(dict.fromkeys(tx_hashes))
    out, topics, need = [], {}, []
    for h in hs:
        v, _ = client.cache.get(f"rcpt3:{client.chain_id}:{token}:{h}") if client.cache else (None, None)
        if isinstance(v, dict):
            out.extend(v["recs"])
            topics[h] = v["topics"]
        else:
            need.append(h)
    res = client.rpc([("eth_getTransactionReceipt", [h]) for h in need])
    for h, rc in zip(need, res):
        if not isinstance(rc, dict):
            client.errors.append(f"rpc receipt missing: {h}")
            continue
        recs, tops = [], {}
        for lg in rc.get("logs") or []:
            tp = lg.get("topics") or []
            if tp:
                tops.setdefault(tp[0], set()).add((lg.get("address") or "").lower())
            if (lg.get("address") or "").lower() != token or len(tp) != 3 or tp[0] != TRANSFER:
                continue
            recs.append({"tx": h, "block": _int(rc.get("blockNumber")), "idx": _int(lg.get("logIndex")),
                         "from": topic_addr(tp[1]), "to": topic_addr(tp[2]), "value": _int(lg.get("data"))})
        tops = {k: sorted(v) for k, v in tops.items()}
        if client.cache:
            client.cache.put(f"rcpt3:{client.chain_id}:{token}:{h}", {"recs": recs, "topics": tops}, client.chain_id, _int(rc.get("blockNumber")))
        out.extend(recs)
        topics[h] = tops
    return out, topics


def tx_records(client, tx_hashes, token):
    """Normalized Transfer records of `token` for the given txs (see tx_records_full)."""
    return tx_records_full(client, tx_hashes, token)[0]
