"""Manual stablecoin order: the customer sends USDC on Base to a receive-only address shown on the checkout page and the
operator confirms the tx hash in /admin. No keys, no sweeping, no automatic on-chain verification in the pilot (the
operator checks the explorer; an RPC check against Base is a later step). The case's deep run becomes leasable only
after `settled`."""
import os
import re

from . import base

HEX_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")


def receive_address():
    """Read-only receive address from the environment; absent means checkout is disabled (never a placeholder address)."""
    a = os.environ.get("SLEUTH_PAY_TO_BASE_USDC", "").strip().lower()
    return a if re.fullmatch(r"0x[0-9a-f]{40}", a) else None


def start(conn, case_id, product="founding_case"):
    pay_to = receive_address()
    if not pay_to:
        raise base.PaymentError("checkout disabled: SLEUTH_PAY_TO_BASE_USDC is not configured")
    row = base.create(conn, "manual", product, idempotency_key=f"manual:{case_id}:{product}", case_id=case_id, pay_to=pay_to)
    if row["state"] == "created":
        row = base.advance(conn, row["id"], "awaiting")
    return row


def confirm(conn, payment_id, tx_hash, operator):
    """Operator attests the transfer landed. Idempotent for the same tx hash; a different hash on a settled row is refused."""
    if not HEX_TX.match(tx_hash or ""):
        raise base.PaymentError("tx hash must be 0x + 64 hex chars")
    row = base.get(conn, payment_id)
    if not row:
        raise base.PaymentError("payment not found")
    if row["state"] in ("settled", "entitled"):
        if (row["tx_hash"] or "").lower() == tx_hash.lower():
            return row
        raise base.PaymentError("already settled with a different tx hash")
    row = base.advance(conn, payment_id, "settled", actor=operator, tx_hash=tx_hash.lower())
    return base.advance(conn, payment_id, "entitled", actor=operator)
