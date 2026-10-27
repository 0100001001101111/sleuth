"""Collectors must return what they collected plus exact coverage: a budget expiry or a capped window never erases
earlier windows, and the drain screen can never read as a clean negative."""
import pytest

from sleuth.client import BudgetExceeded
from sleuth.collectors import Span, launchcall, ring
from sleuth.collectors.logs import scan_topics
from sleuth.collectors.wallet import (
    APPROVAL,
    APPROVAL_FOR_ALL,
    asset_events,
    delegation,
    drain_screen,
)
from sleuth.onchain import TRANSFER, pad

VICTIM = "0x1d61c8801e6349e41a910f632bd2d9142673ff3c"
THIEF = "0xdb62b463fbd400f37b944f36a2032e7370539b80"
TOKEN = "0xb4d3a6097b765d094c0950ac03e917a2773eef4d"


ONE = "0x" + "1".rjust(64, "0")


def log(block, topics, data=ONE, idx=0, address=TOKEN):
    return {"transactionHash": "0x" + f"{block:x}".rjust(64, "0"), "blockNumber": hex(block), "logIndex": hex(idx),
            "address": address, "topics": topics, "data": data, "blockHash": "0x" + "ab" * 32}


class FakeClient:
    """Answers eth_getLogs per window; `fail` maps (window-start, window-end) -> error text; `budget_after` raises after N sub-calls."""
    chain_id = 4663
    cache = None

    def __init__(self, logs=(), fail=None, budget_after=None, code="0x", head=1_000_000):
        self.logs, self.fail, self.budget_after, self.code, self.hb = list(logs), fail or {}, budget_after, code, head
        self.calls = 0
        self.windows = []

    def head(self):
        return {"chain_id": 4663, "block": self.hb, "block_time": 1_793_000_000, "time": 1_793_000_000.0}

    def rpc(self, calls, batch=40, soft=False, errors=None):
        out = []
        for m, p in calls:
            self.calls += 1
            if self.budget_after is not None and self.calls > self.budget_after:
                raise BudgetExceeded("budget")
            if m == "eth_getLogs":
                f = p[0]
                lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
                assert hi - lo + 1 <= 30_000, "address-free filter must stay within the measured 30k cap"
                self.windows.append((lo, hi))
                if (lo, hi) in self.fail:
                    errors.append(self.fail[(lo, hi)])
                    out.append(None)
                    continue
                t1 = f["topics"][1]
                alts = f["topics"][0] if isinstance(f["topics"][0], list) else [f["topics"][0]]
                out.append([lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi and lg["topics"][0] in alts and lg["topics"][1] == t1])
            elif m == "eth_getBlockByNumber":
                out.append({"number": hex(self.hb), "hash": "0x" + "cd" * 32, "timestamp": hex(1_793_000_000)})
            elif m == "eth_getCode":
                out.append(self.code)
            elif m == "eth_getTransactionByHash":
                out.append(getattr(self, "tx", None))
            else:
                out.append(None)
        return out


def test_windowed_scan_keeps_completed_windows_when_budget_expires():
    logs = [log(10, [TRANSFER, pad(VICTIM), pad(THIEF)]), log(70_000, [TRANSFER, pad(VICTIM), pad(THIEF)])]
    c = FakeClient(logs, budget_after=2)                      # third window triggers the deadline
    r = scan_topics(c, [TRANSFER, pad(VICTIM)], Span(0, 89_999), batch=1)
    assert [x["block"] for x in r.items] == [10]
    assert not r.coverage.complete and r.coverage.checked_through == 59_999
    assert r.next_checkpoint == 60_000 and r.gaps[0].kind == "budget"


def test_capped_window_is_split_and_other_failures_become_gaps():
    logs = [log(5, [TRANSFER, pad(VICTIM), pad(THIEF)])]
    c = FakeClient(logs, fail={(0, 29_999): "rpc eth_getLogs: query returned more than 10000 results", (30_000, 59_999): "rpc: no response"})
    r = scan_topics(c, [TRANSFER, pad(VICTIM)], Span(0, 59_999))
    assert [x["block"] for x in r.items] == [5]                 # found after the split retry of [0, 14_999]
    assert r.coverage.missing == [Span(30_000, 59_999)] and r.gaps[0].kind == "rpc_error"
    assert r.next_checkpoint == 30_000


def test_asset_events_classify_by_shape_and_screen_is_never_clean():
    logs = [log(100, [TRANSFER, pad(VICTIM), pad(THIEF)], data="0x" + hex(5 * 10**18)[2:].rjust(64, "0")),
            log(101, [TRANSFER, pad(VICTIM), pad(THIEF), "0x" + "7".rjust(64, "0")], idx=1),   # ERC-721 tokenId 7
            log(90, [APPROVAL, pad(VICTIM), pad(THIEF)], data="0x" + "f" * 64),
            log(91, [APPROVAL_FOR_ALL, pad(VICTIM), pad(THIEF)], data="0x" + "1".rjust(64, "0"))]
    c = FakeClient(logs, code="0xef0100" + THIEF[2:])
    ev = asset_events(c, VICTIM, Span(0, 199))
    kinds = sorted(e["event"] + ":" + e.get("standard", "") for e in ev.items)
    assert kinds == ["approval_event_owner_victim:erc20", "approval_for_all_event_owner_victim:", "transfer_event_from_victim:erc20", "transfer_event_from_victim:erc721"]
    out = drain_screen(c, VICTIM, span=Span(0, 199))
    assert out["predicates"] == {"transfer_event_from_victim": True, "approval_event_owner_victim": True, "delegation_observed_at_block": True}
    assert out["delegation"]["delegate_target"] == THIEF
    assert all(v["verdict"] is None for v in out["causal"].values())          # causes never asserted from events
    assert out["tokens"][TOKEN]["out_raw"] == 5 * 10**18 and out["tokens"][TOKEN]["symbol"] is None
    assert any("internal" in g for g in out["gaps"]) and any("7702" in g for g in out["gaps"])


def test_screen_without_events_is_false_only_when_coverage_is_complete():
    c = FakeClient([], fail={(0, 29_999): "rpc: no response"})
    out = drain_screen(c, VICTIM, span=Span(0, 29_999))
    assert out["predicates"]["transfer_event_from_victim"] is None and out["complete"] is False
    c = FakeClient([])
    out = drain_screen(c, VICTIM, span=Span(0, 29_999))
    assert out["predicates"]["transfer_event_from_victim"] is False and out["predicates"]["delegation_observed_at_block"] is False


def test_delegation_never_uses_cache_and_survives_rpc_failure():
    c = FakeClient([], code="0x")
    assert delegation(c, VICTIM)["delegated"] is False
    c = FakeClient([], budget_after=0)
    d = delegation(c, VICTIM)
    assert d["delegated"] is None and d["gaps"]


def _abi_launch_calldata(exemptions, fee_recipient=THIEF, name="Phago", symbol="PHG", quote=68_888_000_000_000_000):
    """Hand-rolled ABI encoding of launchAndBuy(params, uint256, address, uint256, uint256, address, address[])."""
    def w(n):
        return n.to_bytes(32, "big")
    def addr(a):
        return bytes(12) + bytes.fromhex(a[2:])
    def s(x):
        b = x.encode()
        return w(len(b)) + b + bytes((-len(b)) % 32)
    # params tuple: 5 dynamic (4 strings + socials tuple of 5 strings) then 5 static
    socials = b"".join(w(32 * 5 + 32 * i) for i in range(5)) + b"".join(s("") for _ in range(5))
    heads, tails, cur = [], [], 32 * 10
    for item in (s(name), s(symbol), s(""), s(""), socials):
        heads.append(w(cur)); tails.append(item); cur += len(item)
    params = b"".join(heads) + addr(fee_recipient) + w(150) + w(0) + bytes(32) + bytes(32) + b"".join(tails)
    ex = w(len(exemptions)) + b"".join(addr(a) for a in exemptions)
    head = w(32 * 7) + w(0) + bytes(32) + w(quote) + w(1) + addr(VICTIM) + w(32 * 7 + len(params))
    return launchcall.SELECTOR + (head + params + ex).hex()


def test_launch_calldata_decodes_exemptions_and_fee_recipient():
    ex = ["0x" + f"{i:040x}" for i in range(1, 20)]
    d = launchcall.decode_launch_and_buy(_abi_launch_calldata(ex))
    assert d["snipe_tax_exemptions"] == ex and d["creator_fee_recipient"] == THIEF and d["creator_tax_bps"] == 150
    assert d["name"] == "Phago" and d["symbol"] == "PHG" and d["recipient"] == VICTIM and d["quote_in_raw"] == 68_888_000_000_000_000
    assert launchcall.decode_launch_and_buy("0xdeadbeef") is None
    with pytest.raises(ValueError):
        launchcall.decode_launch_and_buy(launchcall.SELECTOR + "00" * 40)
    c = FakeClient([]); c.tx = {"from": VICTIM, "to": "0xe33e9e479df8802cb0866d5d05258bec4cf62948", "input": _abi_launch_calldata(ex[:3])}
    r = launchcall.launch_calldata(c, "0x" + "1" * 64)
    assert r["found"] and len(r["decoded"]["snipe_tax_exemptions"]) == 3 and r["gaps"] == []


def test_coordinated_launch_is_three_valued():
    market = "0x6f4ac34314b317ee4572e0d54cbecd3210c008d9"
    supply = 1_000_000
    buyers = ["0x" + f"{i:040x}" for i in range(1, 13)]
    recs = [{"from": market, "to": b, "value": 50_000, "block": 100 + (i % 3)} for i, b in enumerate(buyers)]
    recs.append({"from": market, "to": "0x" + "e" * 40, "value": 300_000, "block": 110})   # late buyer, outside cutoff
    r = ring.coordinated_launch(recs, 100, supply, exemptions=buyers[:5], market=market)
    assert r["verdict"] is True and r["early_buyers"] == 12 and r["early_share"] == 0.6 and r["exempt_buyers"] == 5
    r = ring.coordinated_launch(recs[:4], 100, supply, market=market)
    assert r["verdict"] is False
    r = ring.coordinated_launch(recs[:4], 100, supply, market=market, coverage_complete=False)
    assert r["verdict"] is None and "incomplete" in r["reason"]
    assert ring.coordinated_launch([], None, supply)["verdict"] is None


def test_drain_screen_is_incomplete_when_delegation_check_fails():
    """Complete event coverage is not a complete run while the delegation predicate is unknown; and a malformed
    eth_getCode value is unknown, never a 'no'."""
    c = FakeClient([], code=None)   # eth_getCode returns nothing
    out = drain_screen(c, VICTIM, span=Span(0, 29_999))
    assert out["predicates"]["transfer_event_from_victim"] is False   # events are complete on their own
    assert out["predicates"]["delegation_observed_at_block"] is None and out["complete"] is False
    assert delegation(FakeClient([], code=""), VICTIM)["delegated"] is None
    assert delegation(FakeClient([], code="0xzz"), VICTIM)["delegated"] is None
    assert delegation(FakeClient([], code="0x"), VICTIM)["observed_block_hash"] == "0x" + "cd" * 32
