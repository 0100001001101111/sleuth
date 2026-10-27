"""Payment state machine persisted in the payments table:
  created -> awaiting -> verified -> settlement_pending -> settled -> entitled
  any non-terminal -> failed; settled/entitled -> refunded
Only `settled`/`entitled` unlock deep runs (queue.lease checks this). A verified-but-unsettled signature is worth nothing."""
from .. import db

STATES = ("created", "awaiting", "verified", "settlement_pending", "settled", "entitled", "refunded", "failed")
TRANSITIONS = {
    "created": {"awaiting", "verified", "failed"}, "awaiting": {"verified", "settlement_pending", "settled", "failed"},
    "verified": {"settlement_pending", "settled", "failed"}, "settlement_pending": {"settled", "failed"},
    "settled": {"entitled", "refunded"}, "entitled": {"refunded"}, "refunded": set(), "failed": set(),
}
PRODUCTS = {"founding_case": ("29.00", "USDC", "base"), "machine_screen": ("0.10", "USDC", "base")}


class PaymentError(Exception):
    pass


def create(conn, provider, product, idempotency_key, case_id=None, pay_to=None, notes=None):
    """Idempotent on idempotency_key: a double-submitted checkout returns the same payment row."""
    if product not in PRODUCTS:
        raise PaymentError(f"unknown product {product}")
    amount, asset, network = PRODUCTS[product]
    with db.tx(conn):
        row = conn.execute("SELECT * FROM payments WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row:
            return row
        pid = db.new_id()
        conn.execute("INSERT INTO payments (id, case_id, provider, product, amount, asset, network, pay_to, state, idempotency_key, created_at, updated_at, notes)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (pid, case_id, provider, product, amount, asset, network, pay_to, "created", idempotency_key, db.now(), db.now(), notes))
        db.audit(conn, "system", "payment_created", pid, {"provider": provider, "product": product, "case_id": case_id})
        return conn.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()


def get(conn, payment_id):
    return conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()


def advance(conn, payment_id, new_state, actor="system", **fields):
    """Move a payment forward under the lock; illegal transitions raise instead of silently overwriting history.
    A duplicate webhook that re-sends the current state is a no-op (returns the row), not an error."""
    if new_state not in STATES:
        raise PaymentError(f"unknown state {new_state}")
    allowed = {"payer", "provider_ref", "tx_hash", "notes"}
    bad = set(fields) - allowed
    if bad:
        raise PaymentError(f"cannot set {sorted(bad)}")
    with db.tx(conn):
        row = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if not row:
            raise PaymentError("payment not found")
        if row["state"] == new_state:
            return row
        if new_state not in TRANSITIONS[row["state"]]:
            raise PaymentError(f"illegal transition {row['state']} -> {new_state}")
        sets = ", ".join(f"{k}=?" for k in fields)
        try:
            conn.execute(f"UPDATE payments SET state=?, updated_at=?{', ' + sets if sets else ''} WHERE id=?",
                         (new_state, db.now(), *fields.values(), payment_id))
        except db.sqlite3.IntegrityError as e:  # same (network, tx_hash) already credited to another payment
            raise PaymentError(f"tx already used: {e}")
        db.audit(conn, actor, "payment_state", payment_id, {"from": row["state"], "to": new_state, **{k: str(v)[:80] for k, v in fields.items()}})
        return conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
