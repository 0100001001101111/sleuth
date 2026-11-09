"""Launch-dump detection: first-block buyers -> hub contract -> dozens of fresh sellers (DMN/UMBRA pattern), and a clean launch."""
import json, os
import pytest
from sleuth import launchdump
from fixtures import *  # noqa: F401,F403

MARKET = "0x34c891c392d56657b4e52796ac2c13c0788cf10b"
LAUNCHER = "0xdedd67658188488623fd84d3c7a1b2ec5e2fe8c6"
HUB = "0xb06983db4fad9cd94efbf9088c364ebcacde1214"
BOTS = [f"0x{i:040x}" for i in range(0xa1, 0xa6)]
THROW = [f"0x{i:040x}" for i in range(0xc0, 0xc0 + 25)]
SUPPLY = 1_000_000_000 * E
C = 91_132_699


def _dmn_like():
    recs = [rec("0xmint", C, ZERO, MARKET, SUPPLY, 0), rec("0xmint", C, MARKET, LAUNCHER, 10_000_000 * E, 1)]
    for i, b in enumerate(BOTS):  # one tx, five recipients, first trading block
        recs.append(rec("0xsnipe", C + 1, MARKET, b, 56_000_000 * E, i))
    for i, b in enumerate(BOTS):  # forward everything to the hub
        recs.append(rec(f"0xfwd{i}", C + 5, b, HUB, 56_000_000 * E))
    for i, w in enumerate(THROW):  # hub sells through 25 throwaway callers, 37-63 blocks after launch
        recs.append(rec(f"0xsell{i}", C + 37 + i, HUB, MARKET, 11_200_000 * E))
    return recs


def test_dmn_pattern_is_a_launch_dump():
    senders = lambda hs: {h: THROW[int(h[6:])] for h in hs}
    codes = lambda ad: {a: ("contract" if a == HUB else "eoa") for a in ad}
    r = launchdump.analyze(_dmn_like(), MARKET, LAUNCHER, C, SUPPLY, codes, senders, first_blocks=3)
    assert r["launch_dump"] is True
    assert abs(r["share_bought"] - 0.29) < 1e-9           # 280M bots + 10M launcher
    assert r["hubs"] and r["hubs"][0]["addr"] == HUB and r["hubs"][0]["from_n"] == 5
    assert r["sold_via_hub"] == 280_000_000 * E and r["sold_direct"] == 0
    assert r["hub_sell_txs"] == 25 and r["hub_sellers"] == 25 and r["blocks_to_first_sell"] == 37
    assert abs(r["dumped_fraction"] - 280 / 290) < 1e-9
    assert any(b["is_launcher"] for b in r["early_buyers"])


def test_clean_launch_is_not_a_dump():
    recs = [rec("0xmint", C, ZERO, MARKET, SUPPLY, 0), rec("0xmint", C, MARKET, LAUNCHER, 12_000_000 * E, 1)]
    for i in range(6):  # organic buyers later, no forwarding, no sells
        recs.append(rec(f"0xbuy{i}", C + 200 + i * 50, MARKET, SIBS[i % len(SIBS)], 1_000_000 * E))
    r = launchdump.analyze(recs, MARKET, LAUNCHER, C, SUPPLY, lambda ad: {a: "eoa" for a in ad}, lambda hs: {}, first_blocks=3)
    assert r["launch_dump"] is False and r["hubs"] == [] and r["dumped"] == 0
    assert abs(r["share_bought"] - 0.012) < 1e-9


def test_pull_and_sell_in_one_tx_counts_as_via_hub():
    # Real DMN shape: the throwaway caller makes the hub pull from the bot wallet and sell in ONE tx; the bot nets as seller.
    recs = [rec("0xmint", C, ZERO, MARKET, SUPPLY, 0)]
    for i, b in enumerate(BOTS):
        recs.append(rec("0xsnipe", C + 1, MARKET, b, 56_000_000 * E, i))
    k = 0
    for b in BOTS:
        for j in range(5):
            recs.append(rec(f"0xsell{k}", C + 37 + k, b, HUB, 11_200_000 * E, 0))
            recs.append(rec(f"0xsell{k}", C + 37 + k, HUB, MARKET, 11_200_000 * E, 1))
            k += 1
    codes = lambda ad: {a: ("contract" if a == HUB else "eoa") for a in ad}
    senders = lambda hs: {h: THROW[int(h[6:])] for h in hs}
    r = launchdump.analyze(recs, MARKET, None, C, SUPPLY, codes, senders, first_blocks=3)
    assert r["launch_dump"] is True and r["sold_direct"] == 0 and r["sold_via_hub"] == 280_000_000 * E
    assert r["hub_sell_txs"] == 25 and r["hub_sellers"] == 25 and r["hubs"][0]["from_n"] == 5


def test_direct_sell_by_early_buyers_counts_without_a_hub():
    w = SIBS[0]
    recs = [rec("0xmint", C, ZERO, MARKET, SUPPLY, 0), rec("0xb", C + 1, MARKET, w, 100_000_000 * E), rec("0xs", C + 40, w, MARKET, 90_000_000 * E)]
    r = launchdump.analyze(recs, MARKET, None, C, SUPPLY, lambda ad: {a: "eoa" for a in ad}, lambda hs: {}, first_blocks=3)
    assert r["launch_dump"] is True and r["sold_direct"] == 90_000_000 * E and r["hubs"] == []


def test_incomplete_evidence_is_unknown():
    r = launchdump.analyze([], MARKET, None, C, SUPPLY, lambda ad: {}, lambda hs: {})
    assert r["launch_dump"] is None and r["coverage"]["complete"] is False
    r2 = launchdump.analyze(_dmn_like(), MARKET, LAUNCHER, C, None, lambda ad: {a: "contract" for a in ad}, lambda hs: {})
    assert r2["launch_dump"] is None and r2["share_bought"] is None


REAL = "/Users/bo/hedge-fund/reports/hf_tasks_20261017/DAEMON_LOOK/dmn_transfers.json"


@pytest.mark.skipif(not os.path.exists(REAL), reason="hand-reconstructed DMN transfer log not on this machine")
def test_real_dmn_transfers_reproduce_the_hand_finding():
    rows = json.load(open(REAL))
    recs = [{"tx": "0x" + r["tx"], "block": r["block"], "idx": i, "from": r["from"].lower(), "to": r["to"].lower(),
             "value": int(round(r["amt"] * 1_000_000))} for i, r in enumerate(rows)]
    supply = 1_000_000_000 * 1_000_000
    codes = lambda ad: {a: ("contract" if a == HUB else "eoa") for a in ad}
    senders = lambda hs: {h: f"0x{i:040x}" for i, h in enumerate(hs)}  # distinct callers, as found by hand
    r = launchdump.analyze(recs, MARKET, LAUNCHER, C, supply, codes, senders, first_blocks=3, end_block=92173120)
    assert r["launch_dump"] is True
    assert 0.28 <= r["share_bought"] <= 0.30            # FINDINGS.md: 280M (28.0%) to five exempt wallets + 10M creator
    assert [h["addr"] for h in r["hubs"]] == [HUB] and r["hubs"][0]["from_n"] == 5
    assert abs(r["sold_via_hub"] / supply - 0.28) < 0.001 and r["hub_sell_txs"] == 25
    assert r["blocks_to_first_sell"] <= 63


def test_bundled_launch_without_a_dump_is_flagged():
    # VEIL shape (Solana, 2026-10-12): 13 wallets take ~82% in the first slots and do not sell inside the window.
    recs = [rec("0xmint", C, ZERO, MARKET, SUPPLY, 0)]
    wallets = [f"0x{i:040x}" for i in range(0xb0, 0xb0 + 13)]
    for i, w in enumerate(wallets):
        recs.append(rec(f"0xbund{i}", C + 1, MARKET, w, 63_000_000 * E))
    r = launchdump.analyze(recs, MARKET, LAUNCHER, C, SUPPLY, lambda ad: {a: "eoa" for a in ad}, lambda hs: {}, first_blocks=3)
    assert r["bundled"] is True and r["bundle_wallets"] == 13 and abs(r["bundle_share"] - 0.819) < 1e-9
    assert r["launch_dump"] is False and r["dumped"] == 0
    clean = launchdump.analyze([rec("0xmint", C, ZERO, MARKET, SUPPLY, 0), rec("0xb", C + 1, MARKET, LAUNCHER, 12_000_000 * E)],
                               MARKET, LAUNCHER, C, SUPPLY, lambda ad: {a: "eoa" for a in ad}, lambda hs: {})
    assert clean["bundled"] is False and clean["bundle_wallets"] == 0
