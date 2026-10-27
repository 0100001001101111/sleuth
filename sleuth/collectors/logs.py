"""Windowed eth_getLogs for topic-only (address-free) filters. Measured on rpc.mainnet.chain.robinhood.com
(2026-10-26): without an `address` filter the node caps a query at 30,000 blocks ("only 30000 are allowed for this
request; narrow the block range, or add an address filter"); with an address filter the 10M-block / 10k-log limits in
sleuth.onchain apply instead. Every completed window is kept even when a later one fails or the budget expires."""
from ..client import BudgetExceeded
from ..onchain import _CAP_MARKERS, _int, topic_addr
from .types import Coverage, Gap, Result, Span

WINDOW = 30_000       # node-imposed cap for address-free filters
BATCH = 20            # windows per batched round-trip; the throttle counts sub-calls (~14/s)
MIN_WINDOW = 1_000    # stop splitting below this and record a gap instead


def _norm(lg):
    """Raw log -> stable record. Keeps every topic so callers can tell ERC-20 (3 topics) from ERC-721 (4 topics)."""
    try:
        return {"tx": lg["transactionHash"], "block": int(lg["blockNumber"], 16), "idx": int(lg["logIndex"], 16),
                "address": lg["address"].lower(), "topics": [t.lower() for t in lg["topics"]], "data": lg.get("data", "0x"),
                "block_hash": _hash32(lg["blockHash"])}
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise ValueError(f"malformed log: {e}")


def _hash32(h):
    """Canonical 0x + 64 lowercase hex, or ValueError: evidence keys and reorg checks compare these as strings."""
    h = str(h).lower()
    if len(h) != 66 or not h.startswith("0x") or any(c not in "0123456789abcdef" for c in h[2:]):
        raise ValueError(f"bad block hash {h[:20]}")
    return h


def _windows(span, window):
    lo = span.start
    while lo <= span.end:
        hi = min(lo + window - 1, span.end)
        yield Span(lo, hi)
        lo = hi + 1


def scan_topics(client, topics, span, address=None, window=WINDOW, checkpoint=None, batch=BATCH):
    """Collect logs matching `topics` (list; each entry a topic hex, a list of alternatives, or None) over `span`.
    `checkpoint` resumes at that block (windows before it count as completed by the caller's earlier run).
    Returns Result(items=[records], coverage, gaps, next_checkpoint). Never raises on budget/provider failure."""
    if not isinstance(span, Span):
        raise TypeError("span must be a Span")
    start = max(span.start, checkpoint) if checkpoint is not None else span.start
    cov = Coverage(requested=span)
    items, gaps = [], []
    pending = list(_windows(Span(start, span.end), window)) if start <= span.end else []
    stopped = False
    while pending and not stopped:
        group, pending = pending[:batch], pending[batch:]
        calls, errs = [], []
        for w in group:
            f = {"fromBlock": hex(w.start), "toBlock": hex(w.end), "topics": topics}
            if address:
                f["address"] = address
            calls.append(("eth_getLogs", [f]))
        try:
            res = client.rpc(calls, errors=errs)
        except BudgetExceeded:
            gaps.append(Gap("budget", "time budget expired before this window", Span(group[0].start, span.end)))
            pending = group + pending
            stopped = True
            break
        err_i = iter(errs)
        for w, r in zip(group, res):
            if isinstance(r, list):
                try:
                    items.extend(_norm(lg) for lg in r)
                except ValueError as e:
                    gaps.append(Gap("malformed", str(e), w))
                    cov.missing.append(w)
                    continue
                cov.completed.append(w)
                continue
            msg = next(err_i, "rpc: no result")
            if any(m in msg.lower() for m in _CAP_MARKERS) and w.blocks > MIN_WINDOW:
                mid = (w.start + w.end) // 2          # too many logs for this window: split and retry
                pending = [Span(w.start, mid), Span(mid + 1, w.end)] + pending
            else:
                cov.missing.append(w)
                gaps.append(Gap("rpc_error", msg[:200], w))
    for w in pending:   # never scheduled
        cov.missing.append(w)
    cov.completed.sort(key=lambda s: s.start)
    cov.missing.sort(key=lambda s: s.start)
    items.sort(key=lambda x: (x["block"], x["idx"]))
    nxt = None if cov.complete else (cov.missing[0].start if cov.missing else None)
    return Result(items=items, coverage=cov, gaps=gaps, next_checkpoint=nxt)


def addr_topic(t):
    """Indexed address topic -> 0x-address (lowercase)."""
    return topic_addr(t)


def data_uint(data):
    return _int(data) if data and data != "0x" else 0
