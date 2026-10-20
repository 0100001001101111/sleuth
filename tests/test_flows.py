"""Economic-trade netting: routed hops counted once, failed swaps absent, burns and plain transfers separated."""
from sleuth import flows
from fixtures import *  # noqa: F401,F403


def test_routed_sell_counted_once():
    # wallet -> router -> settler -> pool in one tx: ONE sell of 10M, not three transfers
    w = SIBS[0]
    recs = [rec("0xs1", 100, w, ROUTER, 10 * E, 0), rec("0xs1", 100, ROUTER, SETTLER, 10 * E, 1), rec("0xs1", 100, SETTLER, POOL, 10 * E, 2)]
    tr = flows.trades(recs, {POOL}, {DEAD, ZERO})
    assert [(t["addr"], t["kind"], t["amount"]) for t in tr] == [(w, "sell", 10 * E)]
    agg = flows.aggregate(tr)
    assert agg[w]["sold"] == 10 * E and agg[w]["n"] == 1
    assert ROUTER not in agg and SETTLER not in agg


def test_failed_swap_emits_nothing():
    # A reverted swap emits no Transfer logs; receipts-based records are therefore empty -> no trade.
    assert flows.trades([], {POOL}, {DEAD}) == []


def test_buy_vs_sell_vs_transfer_vs_burn():
    w, w2 = SIBS[0], SIBS[1]
    recs = [rec("0xb", 1, POOL, w, 5 * E), rec("0xt", 2, w, w2, 2 * E), rec("0xburn", 3, w, DEAD, 1 * E), rec("0xs", 4, w, POOL, 2 * E)]
    kinds = {(t["tx"], t["addr"]): t["kind"] for t in flows.trades(recs, {POOL}, {DEAD})}
    assert kinds[("0xb", w)] == "buy" and kinds[("0xs", w)] == "sell" and kinds[("0xburn", w)] == "burn"
    assert kinds[("0xt", w)] == "transfer_out" and kinds[("0xt", w2)] == "transfer_in"


def test_burn_registry_is_not_a_seller():
    # RobinhoodBurner pulls tokens via transferFrom and forwards to dead: the burner nets to zero, the wallet burned.
    recs = [rec("0xr", 9, DEV, BURNER, 3 * E, 0), rec("0xr", 9, BURNER, DEAD, 3 * E, 1)]
    tr = flows.trades(recs, {POOL}, {DEAD})
    assert tr == [dict(tx="0xr", block=9, addr=DEV, kind="burn", amount=3 * E, counterparty="dead")]


def test_pool_detection_only_classifies_repeat_counterparties():
    recs = [rec(f"0x{i}", i, POOL, SIBS[i % 3], E) for i in range(6)] + [rec("0xz", 50, "0x" + "c" * 40, "0x" + "d" * 40, E)]
    asked = []
    def lookup(addrs):
        asked.extend(addrs)
        return {a: ("contract" if a == POOL else "eoa") for a in addrs}
    pools, hits, kinds = flows.detect_pools(recs, lookup, known=(), min_txs=3)
    assert pools == {POOL} and set(asked) == {POOL}  # EOAs below the threshold never cost a getCode call
    assert hits[POOL] == 6
