"""Durable job queue on the investigation_runs table. Lease with BEGIN IMMEDIATE + conditional UPDATE (SQLite's
answer to SKIP LOCKED). A run that needs an entitlement is invisible to workers until its case has a settled payment,
so a verified-but-unsettled signature can never start a long investigation. 'partial' is a first-class terminal state."""
import hashlib

from . import db

LEASE_S = 300.0
TERMINAL = ("succeeded", "partial", "failed_terminal", "canceled")
MAX_ATTEMPTS = 3


def input_hash(payload):
    return hashlib.sha256(db.dumps(payload).encode()).hexdigest()[:32]


def enqueue(conn, case_id, kind, payload, collector_version, requires_entitlement=False, budget_s=None):
    """Idempotent on (case, kind, input, version): re-enqueueing the same work returns the existing run id."""
    h = input_hash(payload)
    with db.tx(conn):
        row = conn.execute("SELECT id FROM investigation_runs WHERE case_id=? AND kind=? AND input_hash=? AND collector_version=?",
                           (case_id, kind, h, collector_version)).fetchone()
        if row:
            return row["id"]
        rid = db.new_id()
        conn.execute("INSERT INTO investigation_runs (id, case_id, kind, state, input_hash, collector_version, requires_entitlement, budget_s, created_at, result_json)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (rid, case_id, kind, "queued", h, collector_version, int(requires_entitlement), budget_s, db.now(), db.dumps({"input": payload})))
        return rid


def lease(conn, worker_id, lease_s=LEASE_S):
    """Claim the oldest runnable run: queued, or leased with an expired lease (crashed worker). Returns row or None."""
    t = db.now()
    with db.tx(conn):
        row = conn.execute(
            "SELECT r.id FROM investigation_runs r WHERE (r.state='queued' OR (r.state='leased' AND r.lease_expires_at < ?))"
            " AND r.attempts < ? AND (r.requires_entitlement=0 OR EXISTS (SELECT 1 FROM payments p WHERE p.case_id=r.case_id AND p.state IN ('settled','entitled')))"
            " ORDER BY r.created_at LIMIT 1", (t, MAX_ATTEMPTS)).fetchone()
        if not row:
            return None
        n = conn.execute("UPDATE investigation_runs SET state='leased', leased_by=?, lease_expires_at=?, heartbeat_at=?, attempts=attempts+1,"
                         " started_at=COALESCE(started_at, ?) WHERE id=? AND (state='queued' OR (state='leased' AND lease_expires_at < ?))",
                         (worker_id, t + lease_s, t, t, row["id"], t)).rowcount
        if n != 1:
            return None
        return conn.execute("SELECT * FROM investigation_runs WHERE id=?", (row["id"],)).fetchone()


def heartbeat(conn, run_id, worker_id, lease_s=LEASE_S):
    t = db.now()
    with db.tx(conn):
        return conn.execute("UPDATE investigation_runs SET heartbeat_at=?, lease_expires_at=? WHERE id=? AND leased_by=? AND state='leased'",
                            (t, t + lease_s, run_id, worker_id)).rowcount == 1


def finish(conn, run_id, worker_id, state, result=None, error=None, after=None):
    """Only the holder of a still-valid lease may finish; a stale worker's late result is dropped (returns False).
    `retry_wait` re-queues the run unless attempts are exhausted, in which case it becomes failed_terminal (never
    stranded). `after(conn)` runs inside the same transaction as the terminal update, so derived rows (assertions)
    are committed with the run state or not at all."""
    if state not in TERMINAL and state != "retry_wait":
        raise ValueError(f"bad terminal state {state}")
    t = db.now()
    with db.tx(conn):
        n = conn.execute("UPDATE investigation_runs SET state=?, finished_at=?, result_json=COALESCE(?, result_json), error=?, leased_by=NULL"
                         " WHERE id=? AND leased_by=? AND state='leased' AND lease_expires_at>=?",
                         (state, t, db.dumps(result) if result is not None else None, error, run_id, worker_id, t)).rowcount
        if n != 1:
            return False
        if state == "retry_wait":
            conn.execute("UPDATE investigation_runs SET state=CASE WHEN attempts>=? THEN 'failed_terminal' ELSE 'queued' END,"
                         " error=CASE WHEN attempts>=? THEN 'attempts exhausted: ' || COALESCE(error,'') ELSE error END,"
                         " finished_at=CASE WHEN attempts>=? THEN finished_at ELSE NULL END WHERE id=?", (MAX_ATTEMPTS, MAX_ATTEMPTS, MAX_ATTEMPTS, run_id))
        elif after is not None:
            after(conn)
        return True


def exhaust_stale(conn):
    """Runs whose attempts hit the cap while leased-and-expired become failed_terminal (never silently vanish)."""
    with db.tx(conn):
        return conn.execute("UPDATE investigation_runs SET state='failed_terminal', error='attempts exhausted', finished_at=?"
                            " WHERE state='leased' AND lease_expires_at < ? AND attempts >= ?", (db.now(), db.now(), MAX_ATTEMPTS)).rowcount
