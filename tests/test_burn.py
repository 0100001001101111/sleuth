"""Burn verification must find burns executed through delegated (EIP-7702) / internal calls and classify the destination."""
from sleuth import burn, onchain
from fixtures import *  # noqa: F401,F403


class FakeClient:
    chain_id = "4663"
    cache = None
    def __init__(self, receipt, code="0x", supply=(None, None), explorer=None):
        self.receipt, self.code, self.supply, self.explorer = receipt, code, supply, explorer
        self.errors, self.requests = [], 0
    def rpc(self, calls):
        out = []
        for m, p in calls:
            if m == "eth_getTransactionReceipt": out.append(self.receipt)
            elif m == "eth_getBlockByNumber": out.append({"timestamp": hex(1760925158), "number": p[0]})
            elif m == "eth_getCode": out.append(self.code)
            elif m == "eth_call" and p[0]["data"] == "0x18160ddd": out.append(None)
            elif m == "eth_call" and p[0]["data"] == "0x95d89b41": out.append("0x" + (32).to_bytes(32, "big").hex() + (4).to_bytes(32, "big").hex() + b"STOX".ljust(32, b"\0").hex())
            elif m == "eth_call" and p[0]["data"] == "0x06fdde03": out.append("0x" + (32).to_bytes(32, "big").hex() + (9).to_bytes(32, "big").hex() + b"STOXPREPS".ljust(32, b"\0").hex())
            elif m == "eth_call" and p[0]["data"] == "0x313ce567": out.append(hex(18))
            else: out.append(None)
        return out
    def total_supply(self, token, block="latest"):
        return self.supply[0] if isinstance(block, int) and block % 2 == 0 else self.supply[1]  # even block = 'before' in fixtures
    def remaining(self): return 0
    def get(self, *a, **k): return self.explorer


def _log(token, frm, to, value):
    return {"address": token, "topics": [onchain.TRANSFER, onchain.pad(frm), onchain.pad(to)], "data": hex(value)}


def test_internal_eip7702_burn_found_and_dead_destination_inferred():
    # Real case: launcher's 7702 wallet called execute(); tx.to is the wallet itself, burn happens two calls deep.
    rc = {"from": DEV, "to": DEV, "status": "0x1", "blockNumber": hex(93085198), "blockHash": "0xbh",
          "logs": [_log(TOKEN, DEV, BURNER, 14701043 * E), _log(TOKEN, DEV, DEAD, 14701043 * E)]}
    out = burn.check_tx(FakeClient(rc, code="0xef0100" + "63c0c19a282a1b52b07dd5a65b58948a07dae32b"), BURN_TX, TOKEN)
    assert out["found"] and out["status"] == "ok" and len(out["burns"]) == 1
    assert out["burns"][0]["amount"] == 14701043 * E and out["burns"][0]["to"] == DEAD
    assert out["executed_via_internal_call"] and out["sender_delegated_eip7702"]
    assert out["supply_reduced"] is False and "dead address" in out["burn_type"]


def test_zero_address_burn_inferred_supply_reducing_when_no_archive():
    rc = {"from": DEV, "to": TOKEN, "status": "0x1", "blockNumber": hex(100), "logs": [_log(TOKEN, DEV, ZERO, 5 * E)]}
    out = burn.check_tx(FakeClient(rc), "0xzz", TOKEN)
    assert out["supply_reduced"] is True and out["burn_type"].startswith("supply-reducing")


def test_archive_supply_comparison_wins_over_inference():
    rc = {"from": DEV, "to": TOKEN, "status": "0x1", "blockNumber": hex(101), "logs": [_log(TOKEN, DEV, DEAD, 5 * E)]}
    out = burn.check_tx(FakeClient(rc, supply=(100 * E, 95 * E)), "0xzz", TOKEN)
    assert out["supply_reduced"] is True and out["burn_type"].startswith("supply-reducing burn (block-level")


def test_no_burn_in_plain_transfer_tx():
    rc = {"from": DEV, "to": TOKEN, "status": "0x1", "blockNumber": hex(102), "logs": [_log(TOKEN, DEV, SIBS[0], 5 * E)]}
    out = burn.check_tx(FakeClient(rc), "0xzz", TOKEN)
    assert out["burns"] == [] and "burn_type" not in out
