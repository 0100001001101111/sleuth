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
    recs = [rec(f"0x{i}", i, POOL, SIBS[i % 3], E) for i in range(3)] + [rec(f"0x{i}", i, SIBS[i % 3], POOL, E) for i in range(3, 6)]
    recs += [rec("0xz", 50, "0x" + "c" * 40, "0x" + "d" * 40, E)]
    asked = []
    def lookup(addrs):
        asked.extend(addrs)
        return {a: ("contract" if a == POOL else "eoa") for a in addrs}
    pools, hits, kinds = flows.detect_pools(recs, lookup, known=(), min_txs=3)
    assert pools == {POOL} and set(asked) == {POOL}  # EOAs below the threshold never cost a getCode call
    assert hits[POOL] == (3, 3)


def test_one_directional_sink_is_not_a_pool_so_parking_tokens_is_not_a_sale():
    """STOX 2026-10-19 21:04 PDT: launcher -> fee splitter 0xb1cd…6d4b -> 50% Staking 0x6c92…c931 / 50% 0xc0f2…219c.
    No swap, no ETH out. Staking received in 6 txs from 3 wallets and sent in 2 txs: a sink, not a venue."""
    STK, SPLIT = "0x6c92d274b33e0920add19644f2177d1708aac931", "0xb1cd15fa5ae39f4ebff19a55e2484ff7bb716d4b"
    recs = [rec(f"0xs{i}", 10 + i, SIBS[i % 3], STK, E) for i in range(6)] + [rec(f"0xo{i}", 30 + i, STK, SIBS[i], E // 10) for i in range(2)]
    recs += [rec("0xdev", 93160848, DEV, SPLIT, 8_889_617 * E), rec("0xdist", 93160861, SPLIT, STK, 4_444_808 * E)]
    pools, hits, _ = flows.detect_pools(recs, lambda addrs: {a: "contract" for a in addrs}, known=(POOL,), min_txs=3)
    assert STK not in pools and SPLIT not in pools and pools == {POOL}
    tr = {(t["addr"], t["kind"]) for t in flows.trades(recs, pools, {DEAD})}
    assert (SPLIT, "sell") not in tr and (SPLIT, "transfer_out") in tr and (DEV, "transfer_out") in tr


def test_launch_and_buy_mint_is_not_a_sale():
    # Real Pons launch tx (AMERICA 0xa9cf…): 1B minted 0x0 -> market, then market -> launcher 636M in the same tx.
    # Netting without mint handling made the market look like it *received* 363M (a 'sell' by nobody) and lost the launcher's buy.
    recs = [rec("0xl", 92995318, ZERO, MARKET, 1_000_000_000 * E, 0), rec("0xl", 92995318, MARKET, DEV, 636_363_636 * E, 6)]
    tr = flows.trades(recs, {MARKET}, {DEAD, ZERO})
    assert tr == [dict(tx="0xl", block=92995318, addr=DEV, kind="buy", amount=636_363_636 * E, counterparty="pool")]
    # plain mint straight to a wallet is a transfer_in from 'mint', never a buy or sell
    tr = flows.trades([rec("0xm", 1, ZERO, DEV, 5 * E)], {MARKET}, {DEAD, ZERO})
    assert tr == [dict(tx="0xm", block=1, addr=DEV, kind="transfer_in", amount=5 * E, counterparty="mint")]


def test_fee_on_transfer_sale_splits_into_sale_and_burn():
    w = SIBS[0]
    recs = [rec("0xf", 7, w, POOL, 95 * E, 0), rec("0xf", 7, w, DEAD, 5 * E, 1)]
    tr = sorted(flows.trades(recs, {POOL}, {DEAD}), key=lambda t: t["kind"])
    assert [(t["kind"], t["amount"]) for t in tr] == [("burn", 5 * E), ("sell", 95 * E)]
