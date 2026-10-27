"""Notification outbox. Rows are written inside the same transaction as the evidence that caused them (so a reorg
rollback can delete unsent ones), and delivered later by the worker. The only sender in the pilot writes to a local
JSONL file / log: no email or SMS provider is called and no money is spent. Real channels plug in behind send_one()."""
import json
import logging
from pathlib import Path

from sleuth import config as C

from . import db

log = logging.getLogger("sleuth.service.notify")
MAX_ATTEMPTS = 5


def enqueue(conn, case_id, dedup_key, channel, destination, payload):
    """Call inside an open db.tx(). Returns False when the dedup key already exists (same finalized event)."""
    try:
        conn.execute("INSERT INTO notification_outbox (id, case_id, dedup_key, channel, destination, payload_json, state, created_at)"
                     " VALUES (?,?,?,?,?,?,?,?)", (db.new_id(), case_id, dedup_key, channel, destination, db.dumps(payload), "pending", db.now()))
        return True
    except db.sqlite3.IntegrityError:
        return False


def send_one(row, outfile=None):
    """Deliver a single row. Pilot channels: 'file' (append JSONL under SLEUTH_HOME/data) and 'log'. Raises on failure."""
    rec = {"id": row["id"], "case_id": row["case_id"], "channel": row["channel"], "to": row["destination"], "payload": db.loads(row["payload_json"])}
    if row["channel"] == "log":
        log.info("notification %s", json.dumps(rec, default=str))
        return
    if row["channel"] in ("file", "email", "sms"):  # email/sms are stubs until a provider is chosen (decision B-4/B-10)
        p = Path(outfile) if outfile else C.DATA / "notifications.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        return
    raise ValueError(f"unknown channel {row['channel']}")


CLAIM_S = 60.0


def deliver_pending(conn, limit=50, outfile=None):
    """Worker loop step: claim one pending row at a time (state -> 'sending' under the write lock, so two workers never
    both send it), deliver, mark sent. A crash mid-send leaves 'sending'; those are reclaimed after CLAIM_S and retried,
    so the worst case is a duplicate delivery, never a silent loss. Failures count attempts and keep the row."""
    sent = 0
    with db.tx(conn):
        conn.execute("UPDATE notification_outbox SET state='pending', last_error='reclaimed stale claim' WHERE state='sending' AND claimed_at<?", (db.now() - CLAIM_S,))
    for _ in range(limit):
        with db.tx(conn):
            row = conn.execute("SELECT * FROM notification_outbox WHERE state='pending' AND attempts < ? ORDER BY created_at LIMIT 1", (MAX_ATTEMPTS,)).fetchone()
            if not row:
                break
            n = conn.execute("UPDATE notification_outbox SET state='sending', claimed_at=? WHERE id=? AND state='pending'", (db.now(), row["id"])).rowcount
        if n != 1:
            continue
        try:
            send_one(row, outfile=outfile)
            with db.tx(conn):
                conn.execute("UPDATE notification_outbox SET state='sent', sent_at=?, attempts=attempts+1 WHERE id=? AND state='sending'", (db.now(), row["id"]))
            sent += 1
        except (OSError, ValueError, db.DbError) as e:
            with db.tx(conn):
                conn.execute("UPDATE notification_outbox SET attempts=attempts+1, last_error=?, state=CASE WHEN attempts+1>=? THEN 'failed' ELSE 'pending' END"
                             " WHERE id=?", (str(e)[:200], MAX_ATTEMPTS, row["id"]))
    return sent
