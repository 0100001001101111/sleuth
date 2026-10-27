"""Continuous monitoring (distinct from the CLI's saved watchlist). Per subscription: two cursors.
  provisional  = scanned to the `latest` block, with that block's hash stored; evidence rows marked finality=provisional.
  finalized    = scanned to the `finalized` block; evidence rows marked finality=finalized; ONLY these emit notifications.
Reorg procedure (design round 3, R-4): before extending the provisional cursor, re-fetch the hash of the stored cursor
block; on mismatch walk back to the common ancestor, delete provisional evidence above it and any unsent outbox rows
tied to it, inside one transaction, and re-scan. Dedup keys include the canonical block hash so a replacement canonical
event is a new notification, never suppressed. The subscription enters `degraded` after repeated provider failures and
records lag; it never pretends stale cursors are current."""
import hashlib
import logging

from sleuth.client import ApiError, BudgetExceeded
from sleuth.collectors import COLLECTOR_VERSION, Span
from sleuth.collectors.logs import scan_topics
from sleuth.onchain import TRANSFER, RpcError, pad

from . import db, notify

log = logging.getLogger("sleuth.service.monitor")
MONITOR_VERSION = f"monitor/0.1.0+{COLLECTOR_VERSION}"
REORG_DEPTH_MAX = 512
DEGRADE_AFTER = 3
POLL_S = 120.0
BUDGET_S = 60.0
PROVIDER_ERRORS = (BudgetExceeded, ApiError, RpcError, OSError, ValueError, TypeError, KeyError)


def block_hash(client, number):
    """Canonical hash of a block by number ('latest'/'finalized' tags allowed). Returns (number, hash) or (None, None)."""
    errs = []
    tag = number if isinstance(number, str) else hex(number)
    (blk,) = client.rpc([("eth_getBlockByNumber", [tag, False])], errors=errs)
    if not blk or not blk.get("hash"):
        return None, None
    return int(blk["number"], 16), blk["hash"].lower()


def _event_key(token, lg):
    return f"transfer:{token}:{lg['block_hash']}:{lg['tx']}:{lg['idx']}"


def _record(conn, sub, token, lg, finality):
    """Insert an evidence row (ignore an identical key). Returns True when newly inserted."""
    payload = {"token": token, "from": "0x" + lg["topics"][1][-40:], "to": "0x" + lg["topics"][2][-40:], "value_raw": str(int(lg["data"], 16)),
               "tx": lg["tx"], "block": lg["block"], "block_hash": lg["block_hash"], "idx": lg["idx"]}
    key = _event_key(token, lg)
    h = hashlib.sha256(db.dumps(payload).encode()).hexdigest()
    try:
        conn.execute("INSERT INTO evidence_items (id, case_id, run_id, kind, key, block, block_hash, finality, payload_json, content_hash, observed_at)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (db.new_id(), sub["case_id"], sub["id"], "monitored_transfer", key, lg["block"], lg["block_hash"],
                                                        finality, db.dumps(payload), h, db.now()))
        return True
    except db.sqlite3.IntegrityError:
        if finality == "finalized":  # promote a provisional row that is now final
            conn.execute("UPDATE evidence_items SET finality='finalized' WHERE case_id=? AND key=? AND finality='provisional'", (sub["case_id"], key))
        return False


def _scan(client, token, addrs, lo, hi):
    items, gaps = [], []
    for a in addrs:
        res = scan_topics(client, [TRANSFER, pad(a)], Span(lo, hi))
        items.extend(res.items)
        gaps.extend(g.detail for g in res.gaps)
        if not res.coverage.complete:
            return items, gaps, res.coverage.checked_through
    return items, gaps, hi


def rollback(conn, sub, ancestor_block):
    """Transactional reorg rollback: drop provisional evidence above the common ancestor and unsent outbox rows that
    cite those block hashes. Finalized rows are never touched (a reorg past finality is a chain emergency, not ours)."""
    with db.tx(conn):
        hashes = [r["block_hash"] for r in conn.execute("SELECT DISTINCT block_hash FROM evidence_items WHERE case_id=? AND run_id=? AND finality='provisional' AND block>?",
                                                          (sub["case_id"], sub["id"], ancestor_block))]
        conn.execute("DELETE FROM evidence_items WHERE case_id=? AND run_id=? AND finality='provisional' AND block>?", (sub["case_id"], sub["id"], ancestor_block))
        for h in hashes:
            conn.execute("DELETE FROM notification_outbox WHERE case_id=? AND state='pending' AND dedup_key LIKE ?", (sub["case_id"], f"%:{h}:%"))
        conn.execute("UPDATE monitoring_subscriptions SET provisional_cursor=?, provisional_hash=NULL, updated_at=? WHERE id=?", (ancestor_block, db.now(), sub["id"]))
        db.audit(conn, "monitor", "reorg_rollback", sub["id"], {"ancestor": ancestor_block, "dropped_hashes": hashes})
    return len(hashes)


def common_ancestor(client, conn, sub):
    """Walk provisional evidence blocks downward until a stored hash matches the canonical chain. Returns block number."""
    rows = conn.execute("SELECT DISTINCT block, block_hash FROM evidence_items WHERE case_id=? AND run_id=? AND finality='provisional' ORDER BY block DESC LIMIT ?",
                        (sub["case_id"], sub["id"], REORG_DEPTH_MAX)).fetchall()
    for r in rows:
        _, h = block_hash(client, r["block"])
        if h and h == r["block_hash"]:
            return r["block"]
    return sub["finalized_cursor"] or (sub["provisional_cursor"] or 0) - REORG_DEPTH_MAX


def _boundary(client, through, hi, hi_hash):
    """Hash to store for a cursor at `through`. A full scan anchors on the hash fetched BEFORE the scan; a partial scan
    fetches the boundary block's hash afterwards and then re-checks that the scan target has not moved meanwhile, so a
    cursor hash never describes a chain the scanned logs did not come from. Raises RpcError on any doubt."""
    if through == hi:
        return hi_hash
    _, th = block_hash(client, through)
    _, again = block_hash(client, hi)
    if th is None or again is None:
        raise RpcError("boundary block hash unavailable")
    if again != hi_hash:
        raise RpcError("chain moved during scan; cursor not advanced")
    return th


def tick(client, conn, sub):
    """One poll of one subscription. Provider failures are recorded on the row and returned; persistence failures
    (db.DbError) propagate so the worker loop backs off instead of counting them as provider trouble."""
    fresh = conn.execute("SELECT * FROM monitoring_subscriptions WHERE id=?", (sub["id"],)).fetchone()  # cursors may have moved since the caller read the row
    sub = dict(fresh or sub)
    token, addrs = sub["token"], [a.lower() for a in db.loads(sub["addresses_json"]) or []]
    out = {"sub": sub["id"], "reorg": False, "new_provisional": 0, "new_final": 0, "notified": 0, "error": None}
    try:
        latest_n, latest_h = block_hash(client, "latest")
        final_n, final_h = block_hash(client, "finalized")
        if latest_n is None or final_n is None:
            raise RpcError("head/finalized block unavailable")
        # 1. reorg check on the stored provisional cursor. An unavailable hash is a provider failure, never a reorg.
        pc, ph = sub.get("provisional_cursor"), sub.get("provisional_hash")
        if pc is not None and ph:
            _, now_h = block_hash(client, pc)
            if now_h is None:
                raise RpcError(f"hash of cursor block {pc} unavailable; reorg check skipped")
            if now_h != ph:
                anc = common_ancestor(client, conn, sub)
                rollback(conn, sub, anc)
                out["reorg"], pc = True, anc
                sub["provisional_cursor"] = anc
        # 2. finalized scan + notifications (finalized-only). All RPC happens before the transaction opens.
        fc = sub.get("finalized_cursor")
        f_lo = (fc + 1) if fc is not None else int(sub["start_block"]) if sub.get("start_block") else max(0, final_n - 1)
        if f_lo <= final_n:
            items, gaps, through = _scan(client, token, addrs, f_lo, final_n)
            th = _boundary(client, through, final_n, final_h)
            with db.tx(conn):
                for lg in items:
                    if lg["block"] > through:
                        continue
                    _record(conn, sub, token, lg, "finalized")
                    key = f"alert:{_event_key(token, lg)}"  # dedup on canonical block hash: a reorged twin is a new alert
                    if notify.enqueue(conn, sub["case_id"], key, sub.get("channel") or "file", sub.get("destination"),
                                      {"kind": "monitored_transfer", "finality": "finalized", "token": token, "tx": lg["tx"], "block": lg["block"],
                                       "block_hash": lg["block_hash"], "from": "0x" + lg["topics"][1][-40:], "to": "0x" + lg["topics"][2][-40:]}):
                        out["notified"] += 1
                    out["new_final"] += 1
                conn.execute("UPDATE monitoring_subscriptions SET finalized_cursor=?, finalized_hash=?, updated_at=? WHERE id=?", (through, th, db.now(), sub["id"]))
            if gaps:
                raise RpcError("; ".join(gaps)[:200])
        # 3. provisional scan (no notifications)
        p_lo = (pc + 1) if pc is not None else (final_n + 1)
        if p_lo <= latest_n:
            items, gaps, through = _scan(client, token, addrs, p_lo, latest_n)
            th = _boundary(client, through, latest_n, latest_h)
            with db.tx(conn):
                for lg in items:
                    if lg["block"] <= through and _record(conn, sub, token, lg, "provisional"):
                        out["new_provisional"] += 1
                conn.execute("UPDATE monitoring_subscriptions SET provisional_cursor=?, provisional_hash=?, updated_at=? WHERE id=?", (through, th, db.now(), sub["id"]))
            if gaps:
                raise RpcError("; ".join(gaps)[:200])
        with db.tx(conn):
            conn.execute("UPDATE monitoring_subscriptions SET state=CASE WHEN ends_at<? THEN 'ended' ELSE 'active' END, last_success_at=?, last_error=NULL,"
                         " lag_blocks=?, next_due_at=?, updated_at=? WHERE id=?", (db.now(), db.now(), 0, db.now() + POLL_S, db.now(), sub["id"]))
    except PROVIDER_ERRORS as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        with db.tx(conn):
            row = conn.execute("SELECT last_error FROM monitoring_subscriptions WHERE id=?", (sub["id"],)).fetchone()
            fails = (int((row["last_error"] or "0|").split("|", 1)[0]) if row and row["last_error"] else 0) + 1
            state = "degraded" if fails >= DEGRADE_AFTER else "active"
            conn.execute("UPDATE monitoring_subscriptions SET state=?, last_error=?, next_due_at=?, updated_at=? WHERE id=? AND state!='ended'",
                         (state, f"{fails}|{out['error']}", db.now() + POLL_S, db.now(), sub["id"]))
    return out


def due(conn, limit=20):
    """Subscriptions to poll now. Expired ones are marked ended first and never scanned again."""
    with db.tx(conn):
        conn.execute("UPDATE monitoring_subscriptions SET state='ended', updated_at=? WHERE state IN ('active','degraded') AND ends_at<=?", (db.now(), db.now()))
    return conn.execute("SELECT * FROM monitoring_subscriptions WHERE state IN ('active','degraded') AND next_due_at<=? ORDER BY next_due_at LIMIT ?",
                        (db.now(), limit)).fetchall()
