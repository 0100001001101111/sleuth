"""Money trail collector: paid vs unpaid disposals, FIFO proceeds attribution, hop following, three-valued verdicts,
budget / fan-out / truncation / high-volume gaps."""
from sleuth.client import BudgetExceeded
from sleuth.collectors.money import money_trail

TOKEN = "0xd56a6c45941ee8aae31ce6a30bd049e0f7489cbd"
DEV = "0x3883fb5a4abfdf91e63936a5c5393deaac998b01"
GAME = "0x05a3a6c50edb78ba65cb70ccc4b40f7426f0ecdf"      # contract that takes the token and pays nothing
POOL = "0x8366a39cc670b4001a1121b8f6a443a643e40951"      # contract that pays ETH for the token
BRIDGE = "0xf70da97812cb96acdf810712aa562db8dfa3dbef"    # known service: Relay bridge solver
USDG = "0x4444444444444444444444444444444444444444"
MULE = "0x1111111111111111111111111111111111111111"
MULE2 = "0x2222222222222222222222222222222222222222"
BOT = "0x0876e530b9b2bccb8f803690af9988882c09a938"
CFG = {"known_services": {BRIDGE: "Relay bridge solver"}, "service_name_markers": ["router", "pool", "bridge"], "service_tx_threshold": 20000}
ETH = lambda raw: {"symbol": "ETH", "raw": str(raw), "decimals": 18}


def _tt(frm, to, raw, ts, tx, token=TOKEN, sym="RAID", typ="ERC-20"):
    return {"from": {"hash": frm}, "to": {"hash": to}, "timestamp": ts, "transaction_hash": tx,
            "token": {"address_hash": token, "symbol": sym, "decimals": "18", "type": typ}, "total": {"value": str(raw)}}


def _tx(frm, to, wei, ts, h, status="ok"):
    return {"from": {"hash": frm}, "to": {"hash": to}, "value": str(wei), "timestamp": ts, "hash": h, "status": status, "method": None}


def _it(frm, to, wei, ts, h):
    return {"from": {"hash": frm}, "to": {"hash": to}, "value": str(wei), "timestamp": ts, "transaction_hash": h, "success": True}


class FakeExplorer:
    """paginate() answers per (address, page kind); `truncate` marks pages as incomplete; `budget_after` raises."""
    chain_id = 4663
    cfg = CFG

    def __init__(self, pages, meta, truncate=(), budget_after=None):
        self.pages, self.meta, self.truncate, self.budget_after = pages, meta, set(truncate), budget_after
        self.calls = 0

    def paginate(self, path, max_pages=5, stop=None):
        self.calls += 1
        if self.budget_after is not None and self.calls > self.budget_after:
            raise BudgetExceeded("budget")
        _, _, addr, what = path.split("/")
        return list(self.pages.get((addr.lower(), what), [])), (addr.lower(), what) not in self.truncate

    def address(self, a):
        m = dict(self.meta.get(a.lower(), {"is_contract": False}))
        m["hash"] = a.lower()
        return m

    def counters(self, a):
        return {"tx": self.meta.get(a.lower(), {}).get("tx", 5)}


T1, T2, T3, T4 = "2026-10-26T15:51:48.000000Z", "2026-10-27T03:09:59.000000Z", "2026-10-27T04:00:00.000000Z", "2026-10-27T05:00:00.000000Z"
META = {GAME: {"is_contract": True, "name": None}, POOL: {"is_contract": True, "name": "PoolManager"}, BRIDGE: {"is_contract": False},
        BOT: {"is_contract": False, "tx": 259_021}}


def test_unpaid_disposal_is_a_transfer_not_a_sale():
    cl = FakeExplorer({(DEV, "token-transfers"): [_tt(DEV, GAME, 569_373_311 * 10**18, T1, "0xa")],
                       (DEV, "transactions"): [], (DEV, "internal-transactions"): []}, META)
    r = money_trail(cl, TOKEN, [(DEV, "launcher")])
    w = r["wallets"][0]
    assert w["disposals"][0]["classification"] == "transfer_to_contract" and w["disposals"][0]["paid"] == []
    assert w["proceeds"] == {} and w["hops"] == [] and w["complete"]
    # nothing was ever paid, so the two proceeds questions have no answer; the other three are firm negatives
    assert r["complete"] and r["verdict"] == {"sale_proceeds_received": False, "token_parked_in_non_venue_contract": True,
                                              "token_sent_to_bridge": False, "proceeds_left_wallet": None, "proceeds_reached_bridge_or_exchange": None}


def test_paid_sale_then_hops_to_bridge_and_holding_wallet():
    pages = {(DEV, "token-transfers"): [_tt(DEV, POOL, 10**21, T2, "0xs")],
             (DEV, "internal-transactions"): [_it(POOL, DEV, 5 * 10**17, T2, "0xs")],
             (DEV, "transactions"): [_tx(DEV, MULE, 3 * 10**17, T3, "0xh1"), _tx(DEV, BRIDGE, 10**17, T3, "0xh2"),
                                     _tx(DEV, MULE2, 10**17, "2026-10-20T00:00:00.000000Z", "0xold")],  # before the sale: not proceeds
             (MULE, "transactions"): [_tx(MULE, MULE2, 2 * 10**17, T4, "0xh3")], (MULE, "token-transfers"): [],
             (MULE2, "transactions"): [], (MULE2, "token-transfers"): []}
    cl = FakeExplorer(pages, META)
    r = money_trail(cl, TOKEN, [(DEV, "launcher")], depth=2)
    w = r["wallets"][0]
    assert w["disposals"][0]["classification"] == "sale" and w["proceeds"]["native"]["raw"] == str(5 * 10**17)
    stops = {h["to"]: h["stop"] for h in w["hops"]}
    assert stops == {MULE: "forwarded", BRIDGE: "exit"}
    assert w["hops"][0]["children"][0]["to"] == MULE2 and w["hops"][0]["children"][0]["stop"] == "holding"
    # MULE kept 0.1 of the 0.3 it got and MULE2 holds 0.2: both are 'holding'; the bridge leg is the exit
    assert w["endpoints"] == {"holding": {"native": ETH(3 * 10**17)}, "exit": {"native": ETH(10**17)}}
    assert r["complete"] and r["verdict"]["proceeds_reached_bridge_or_exchange"] is True and r["verdict"]["proceeds_left_wallet"] is True


def test_only_credited_assets_are_followed_and_only_up_to_the_proceeds():
    # one receipt with two split disposals and one ETH credit: counted once; an ERC-721 'credit' is not a payment;
    # outflows beyond the proceeds total (and an unrelated USDG payment) are not attributed to the sale
    pages = {(DEV, "token-transfers"): [_tt(DEV, POOL, 10**21, T2, "0xs"), _tt(DEV, POOL, 10**21, T2, "0xs"),
                                         _tt(GAME, DEV, 1, T2, "0xs", token="0x5555555555555555555555555555555555555555", sym="NFT", typ="ERC-721"),
                                         _tt(DEV, MULE2, 7 * 10**6, T4, "0xu", token=USDG, sym="USDG")],
             (DEV, "internal-transactions"): [_it(POOL, DEV, 10**17, T2, "0xs")],
             (DEV, "transactions"): [_tx(DEV, MULE, 6 * 10**16, T3, "0xh1"), _tx(DEV, MULE2, 6 * 10**16, T4, "0xh2"), _tx(DEV, MULE2, 10**16, T4, "0xh3")],
             (MULE, "transactions"): [], (MULE, "token-transfers"): [],
             (MULE2, "transactions"): [], (MULE2, "token-transfers"): []}
    r = money_trail(FakeExplorer(pages, META), TOKEN, [(DEV, "launcher")])
    w = r["wallets"][0]
    assert [d["classification"] for d in w["disposals"]] == ["sale", "sale"] and w["proceeds"] == {"native": ETH(10**17)}
    assert [(h["to"], h["attributed"]) for h in w["hops"]] == [(MULE, str(6 * 10**16)), (MULE2, str(4 * 10**16))]
    assert w["not_attributed"] == 1 and w["endpoints"] == {"holding": {"native": ETH(10**17)}}
    assert r["complete"] and r["verdict"]["proceeds_reached_bridge_or_exchange"] is False


def test_depth_and_fanout_caps_make_verdicts_unknown_not_negative():
    outs = [_tx(DEV, f"0x{i:040x}", 10**15, T3, f"0xh{i}") for i in range(1, 9)]
    pages = {(DEV, "token-transfers"): [_tt(DEV, POOL, 10**21, T2, "0xs")], (DEV, "internal-transactions"): [_it(POOL, DEV, 10**16, T2, "0xs")],
             (DEV, "transactions"): outs}
    for i in range(1, 9):
        pages[(f"0x{i:040x}", "transactions")] = [_tx(f"0x{i:040x}", MULE, 10**14, T4, f"0xn{i}")]
        pages[(f"0x{i:040x}", "token-transfers")] = []
    r = money_trail(cl := FakeExplorer(pages, META), TOKEN, [(DEV, "launcher")], depth=0, fanout=6)
    w = r["wallets"][0]
    assert [h["stop"] for h in w["hops"]] == ["depth"] * 6 + ["fanout"] * 2
    assert not r["complete"] and {g["kind"] for g in r["gaps"]} == {"fanout", "depth"}
    assert r["verdict"]["proceeds_reached_bridge_or_exchange"] is None and r["verdict"]["proceeds_left_wallet"] is True
    assert cl.calls == 3  # capped hops are never fetched


def test_truncated_page_and_budget_keep_partial_results():
    pages = {(DEV, "token-transfers"): [_tt(DEV, GAME, 10**18, T1, "0xa")], (DEV, "transactions"): [],
             (DEV, "internal-transactions"): []}
    r = money_trail(FakeExplorer(pages, META, truncate=[(DEV, "transactions")]), TOKEN, [(DEV, "launcher")])
    w = r["wallets"][0]
    # a truncated page could hide the payment: the disposal is 'unverified', never 'parked', and nothing is a firm no
    assert w["disposals"][0]["classification"] == "unverified" and w["complete"] is False
    assert not r["complete"] and r["gaps"][0]["kind"] == "truncated" and all(v is None for v in r["verdict"].values())
    r = money_trail(FakeExplorer(pages, META, budget_after=2), TOKEN, [(DEV, "launcher"), (MULE, "top_seller")])
    assert [w["addr"] for w in r["wallets"]] == [DEV] and r["wallets"][0]["complete"] is False and r["gaps"][-1]["kind"] == "budget"
    assert all(v is None for v in r["verdict"].values())


def test_high_volume_wallet_is_listed_not_followed_and_only_a_known_bridge_counts_as_exit():
    pages = {(MULE, "token-transfers"): [_tt(MULE, BRIDGE, 10**20, T3, "0xb"), _tt(MULE, BOT, 10**20, T3, "0xc")],
             (MULE, "transactions"): [], (MULE, "internal-transactions"): []}
    cl = FakeExplorer(pages, META)
    r = money_trail(cl, TOKEN, [(BOT, "top_seller"), (MULE, "top_seller")])
    assert r["wallets"][0]["skipped"].startswith("high-volume") and r["wallets"][0]["disposals"] == []
    cls = [d["classification"] for d in r["wallets"][1]["disposals"]]
    assert cls == ["sent_to_bridge", "transfer"]   # the busy unlabeled EOA is not a bridge, whatever its label says
    assert r["verdict"]["token_sent_to_bridge"] is True and r["verdict"]["sale_proceeds_received"] is None   # skipped wallet = gap
    assert not r["complete"] and cl.calls == 3


def test_odd_explorer_shapes_become_gaps_not_crashes():
    pages = {(DEV, "token-transfers"): [None, {"from": {"hash": DEV}, "to": "x", "token": "RAID", "total": "1", "timestamp": "garbage", "transaction_hash": "0xz"},
                                         _tt(DEV, GAME, 10**18, "not a time", "0xa")],
             (DEV, "transactions"): [_tx(DEV, MULE, "1e18", T3, "0xh1")], (DEV, "internal-transactions"): []}
    r = money_trail(FakeExplorer(pages, META), TOKEN, [(DEV, "launcher")])
    assert r["wallets"][0]["disposals"] == [] and not r["complete"]
    assert {g["kind"] for g in r["gaps"]} == {"provider", "malformed"} and all(v is None for v in r["verdict"].values())


def test_name_marked_service_contract_is_a_service_not_a_high_volume_wallet():
    router = "0x07964f130000000000000000000000000000000a"
    pages = {(DEV, "token-transfers"): [_tt(DEV, POOL, 10**21, T2, "0xs")], (DEV, "internal-transactions"): [_it(POOL, DEV, 10**17, T2, "0xs")],
             (DEV, "transactions"): [_tx(DEV, router, 10**17, T3, "0xh1")]}
    meta = dict(META, **{router: {"is_contract": True, "name": "TransitSwapRouterV5"}})
    r = money_trail(FakeExplorer(pages, meta), TOKEN, [(DEV, "launcher")])
    w = r["wallets"][0]
    assert w["hops"][0]["stop"] == "service" and w["endpoints"] == {"service": {"native": ETH(10**17)}}
    assert r["complete"] and r["verdict"]["proceeds_reached_bridge_or_exchange"] is False  # a router is not an exit
