"""x402 'exact' scheme gate for the stateless machine screen (feature flag SLEUTH_X402=1). Design round 3, R-3 as
amended by Codex: durable idempotency keyed by (payment_id, request_hash, collector_version); the response is computed
and CACHED before settlement; a crash between settle and commit is reconciled against the facilitator on retry; we never
blindly resettle. One payment_id is bound to exactly one (request_hash, collector_version): presenting it for a
different request is refused before verify/compute, so a settled payment can never be stretched over a second screen.
The facilitator is an injected object with verify(payment, requirements), settle(payment, requirements) ->
{"success", "transaction", "network"} and lookup(payment_id) -> settled tx or None; the pilot ships a MockFacilitator
only. Real facilitators (CDP) need an account: decision B-4, not configured here.

NOTE (Codex docs review): this request/response shape is the pilot's own, not the published x402 wire format
(network should be a CAIP-2 id such as eip155:8453 and the receipt belongs in a PAYMENT-RESPONSE header). Adapting to a
real facilitator is part of B-4."""
import hashlib
import json
import os

from .. import db
from .base import PaymentError

SCHEME, NETWORK, ASSET_NAME = "exact", "base", "USDC"
PROTOCOL_VERSION = 1
CLAIM_S = 300.0  # a 'computing' claim older than this belongs to a dead process and may be taken over


class MockFacilitator:
    """Deterministic stand-in: a payment whose `signature` starts with 'ok' verifies; 'crash' settles but raises before
    returning (simulates the crash boundary); anything else fails. Keeps a settled ledger for reconcile. It does NOT
    check amount or recipient: a real facilitator must."""
    def __init__(self):
        self.settled = {}

    def verify(self, payment, req):
        return {"isValid": str(payment.get("signature", "")).startswith(("ok", "crash")), "invalidReason": None}

    def settle(self, payment, req):
        pid = payment["payment_id"]
        if pid in self.settled:
            raise RuntimeError("double settle attempted")
        self.settled[pid] = {"success": True, "transaction": "0x" + hashlib.sha256(pid.encode()).hexdigest(), "network": NETWORK}
        if str(payment.get("signature", "")).startswith("crash"):
            raise ConnectionError("simulated crash after settlement")
        return self.settled[pid]

    def lookup(self, pid):
        return self.settled.get(pid)


def enabled():
    return os.environ.get("SLEUTH_X402", "") == "1"


def requirements(resource, pay_to, price="0.10"):
    return {"scheme": SCHEME, "network": NETWORK, "maxAmountRequired": price, "asset": ASSET_NAME, "payTo": pay_to, "resource": resource,
            "description": "Sleuth stateless token screen", "mimeType": "application/json", "maxTimeoutSeconds": 60}


def request_hash(params):
    return hashlib.sha256(db.dumps(params).encode()).hexdigest()[:32]


def parse_header(h):
    """PAYMENT-SIGNATURE header carries JSON; it must include a payment_id (nonce) and signature. Returns dict or None."""
    try:
        p = json.loads(h)
        if not isinstance(p, dict) or not p.get("payment_id") or not p.get("signature"):
            return None
        if not isinstance(p["payment_id"], str) or len(p["payment_id"]) > 128:
            return None
        return p
    except (ValueError, TypeError):
        return None


def _record_settlement(conn, key, payment, req, tx):
    """The single place a settlement is persisted (normal and reconcile paths): replay row -> settled, plus one payments
    ledger row per payment_id. A tx hash already credited to another payment is a ledger conflict, never ignored."""
    pid = payment["payment_id"]
    with db.tx(conn):
        conn.execute("UPDATE x402_replays SET state='settled', settle_ref=?, updated_at=? WHERE key=?", (tx, db.now(), key))
        existing = conn.execute("SELECT id, tx_hash FROM payments WHERE idempotency_key=?", (f"x402:{pid}",)).fetchone()
        if existing:
            if (existing["tx_hash"] or "").lower() != tx.lower():
                raise PaymentError(f"payment {pid} already recorded with a different settlement tx")
            return
        try:
            conn.execute("INSERT INTO payments (id, case_id, provider, product, amount, asset, network, pay_to, payer, provider_ref, tx_hash, state, idempotency_key, created_at, updated_at)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (db.new_id(), None, "x402", "machine_screen", req["maxAmountRequired"], ASSET_NAME, NETWORK, req["payTo"],
                                                                   payment.get("payer"), pid, tx.lower(), "settled", f"x402:{pid}", db.now(), db.now()))
        except db.sqlite3.IntegrityError as e:  # (network, tx_hash) unique: this tx already paid for something else
            raise PaymentError(f"settlement tx {tx[:18]}… already credited to another payment: {e}")
        db.audit(conn, "x402", "payment_settled", pid, {"tx": tx, "key": key})


def gate(conn, facilitator, payment, req, params, collector_version, compute):
    """Run the idempotent paid request. Returns (status, body). Status 402 = not paid / invalid / in progress;
    200 = result (and the settlement receipt in body['payment']). `compute()` is called at most once per replay key
    unless the process holding a 'computing' claim died (claim older than CLAIM_S): then one retry recomputes."""
    pid, rh = payment["payment_id"], request_hash({"params": params, "requirements": req})  # price/payTo are part of the binding
    key = f"{pid}:{rh}:{collector_version}"
    busy = {"error": "this payment is being processed; retry shortly with the same payment_id"}
    bound = {"error": "payment_id already bound to a different request; use a fresh payment", "accepts": [req]}
    # one payment_id <-> one request (enforced by UNIQUE(payment_id); this lookup only gives the clearer message)
    other = conn.execute("SELECT key FROM x402_replays WHERE payment_id=? AND key!=?", (pid, key)).fetchone()
    if other:
        return 402, bound
    row = conn.execute("SELECT * FROM x402_replays WHERE key=?", (key,)).fetchone()
    reclaimed = False
    if row and row["state"] == "computing":  # a claim whose owner died is taken over once; a live one is told to retry
        with db.tx(conn):
            n = conn.execute("UPDATE x402_replays SET updated_at=? WHERE key=? AND state='computing' AND updated_at<?",
                             (db.now(), key, db.now() - CLAIM_S)).rowcount
        if n != 1:
            return 402, busy
        row, reclaimed = None, True
    if row and row["state"] == "settled":
        return 200, dict(db.loads(row["response_json"]), payment={"settled": True, "transaction": row["settle_ref"], "replay": True})
    if row and row["state"] == "failed":
        return 402, {"error": "settlement failed for this payment; use a fresh payment", "accepts": [req]}
    if row and row["state"] == "computed":  # crashed between settle and commit, or settle raised: reconcile first
        found = facilitator.lookup(pid)
        response = db.loads(row["response_json"])
        if found:
            try:
                _record_settlement(conn, key, payment, req, found["transaction"])
            except PaymentError as e:
                return 402, {"error": str(e)}
            return 200, dict(response, payment={"settled": True, "transaction": found["transaction"], "reconciled": True})
        # not settled anywhere: safe to settle now (below) without recomputing
    else:
        v = facilitator.verify(payment, req)
        if not v.get("isValid"):
            return 402, {"error": "payment invalid", "reason": v.get("invalidReason"), "accepts": [req]}
        if not reclaimed:
            try:  # atomic claim: a concurrent duplicate hits the primary key (same request) or UNIQUE(payment_id) (other request)
                with db.tx(conn):
                    conn.execute("INSERT INTO x402_replays (key, payment_id, request_hash, collector_version, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                                 (key, pid, rh, collector_version, "computing", db.now(), db.now()))
            except (db.DbError, db.sqlite3.IntegrityError) as e:
                if "UNIQUE" not in str(e) and "PRIMARY" not in str(e):
                    raise
                taken = conn.execute("SELECT key FROM x402_replays WHERE payment_id=?", (pid,)).fetchone()
                return 402, (bound if taken and taken["key"] != key else busy)
        try:
            response = compute()
        except Exception as e:  # noqa: BLE001 - release the claim so the same payment can retry; nothing was charged
            with db.tx(conn):
                conn.execute("DELETE FROM x402_replays WHERE key=? AND state='computing'", (key,))
            return 503, {"error": "screen failed before settlement; nothing was charged", "detail": f"{type(e).__name__}: {str(e)[:120]}"}
        with db.tx(conn):
            conn.execute("UPDATE x402_replays SET state='computed', response_json=?, updated_at=? WHERE key=?", (db.dumps(response), db.now(), key))
    try:
        s = facilitator.settle(payment, req)
    except Exception as e:  # noqa: BLE001 - any failure here is 'uncertain', the row stays 'computed' and the next call reconciles
        return 402, {"error": "settlement uncertain; retry with the same payment_id to reconcile", "detail": f"{type(e).__name__}: {str(e)[:120]}"}
    if not s.get("success"):
        with db.tx(conn):
            conn.execute("UPDATE x402_replays SET state='failed', updated_at=? WHERE key=?", (db.now(), key))
        return 402, {"error": "settlement failed", "accepts": [req]}
    try:
        _record_settlement(conn, key, payment, req, s["transaction"])
    except PaymentError as e:
        return 402, {"error": str(e)}
    return 200, dict(response, payment={"settled": True, "transaction": s["transaction"]})
