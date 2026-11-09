"""Solana support: base58/PDA math, balance-delta records, and a pump.fun launch-and-dump through a fake public node."""
import json
from sleuth import solana as S, launchdump as LD, flows


def test_base58_roundtrip_and_pda_is_off_curve():
    assert S.b58decode(S.SYSTEM) == b"\x00" * 32 and S.b58encode(b"\x00" * 32) == S.SYSTEM
    assert len(S.b58decode(S.PUMP)) == 32 and S.b58encode(S.b58decode(S.PUMP)) == S.PUMP
    mint = "Aov3BtQvrGXM5V9oiHu8PKJWeV8a2rFnr24fwWcNpump"
    curve, nonce = S.find_program_address([b"bonding-curve", S.b58decode(mint)], S.PUMP)
    assert len(S.b58decode(curve)) == 32 and not S.on_curve(S.b58decode(curve)) and 0 <= nonce <= 255
    assert S.bonding_curve(mint) == curve  # deterministic
    # a real ed25519 public key (the System program id is the identity-free all-zero key, which IS on the curve)
    assert S.on_curve(b"\x00" * 32)


MINT = "Aov3BtQvrGXM5V9oiHu8PKJWeV8a2rFnr24fwWcNpump"
CURVE = S.bonding_curve(MINT)
DEV, S1, S2, W = "Dev11111111111111111111111111111111111111111", "Snip1111111111111111111111111111111111111111", "Snip2222222222222222222222222222222222222222", "Wal11111111111111111111111111111111111111111"
E = 10 ** 6
SUPPLY = 1_000_000_000 * E


def ptx(slot, sig, payer, pre, post, sol=None):
    """Parsed-tx shape: pre/post = {owner: amount}."""
    owners = sorted(set(pre) | set(post))
    keys = [{"pubkey": payer, "signer": True}] + [{"pubkey": o, "signer": False} for o in owners if o != payer] + [{"pubkey": S.PUMP, "signer": False}]
    tb = lambda d: [{"accountIndex": i, "mint": MINT, "owner": o, "uiTokenAmount": {"amount": str(v), "decimals": 6}} for i, (o, v) in enumerate(d.items())]
    sol = sol or {}
    pre_b = [1_000_000_000 for _ in keys]
    post_b = [1_000_000_000 + sol.get(k["pubkey"], 0) for k in keys]
    return {"slot": slot, "blockTime": 1_700_000_000 + slot, "meta": {"err": None, "preTokenBalances": tb(pre), "postTokenBalances": tb(post),
            "preBalances": pre_b, "postBalances": post_b}, "transaction": {"message": {"accountKeys": keys}}}


def test_tx_records_net_per_owner_with_mint_and_burn():
    tx = ptx(10, "sigA", W, {CURVE: 500 * E, W: 0}, {CURVE: 400 * E, W: 100 * E})
    recs, sol = S.tx_records("sigA", tx, MINT)
    assert recs == [{"tx": "sigA", "block": 10, "idx": 0, "from": CURVE, "to": W, "value": 100 * E}]
    mint_tx = ptx(1, "sigM", DEV, {}, {CURVE: SUPPLY})
    recs, _ = S.tx_records("sigM", mint_tx, MINT)
    assert recs[0]["from"] == S.SYSTEM and recs[0]["to"] == CURVE and recs[0]["value"] == SUPPLY
    burn_tx = ptx(2, "sigB", W, {W: 50 * E}, {W: 0})
    recs, _ = S.tx_records("sigB", burn_tx, MINT)
    assert recs[0]["to"] == S.INCINERATOR
    failed = dict(mint_tx); failed["meta"] = dict(mint_tx["meta"], err={"InstructionError": []})
    assert S.tx_records("x", failed, MINT) == ([], {})


class FakeNode:
    """Answers the handful of RPC methods the module uses, from a scripted pump.fun launch."""

    def __init__(self, txs):
        self.txs = txs  # sig -> parsed tx
        self.requests, self.errors, self.cache, self.chain = 0, [], None, {"known_pools": [], "tz": "UTC"}
        self.explorer, self.chain_id = "https://solscan.io", "solana"

    def rpc(self, calls, batch=20):
        out = []
        for m, p in calls:
            self.requests += 1
            if m == "getSlot": out.append(5000)
            elif m == "getBlockTime": out.append(1_700_000_000 + p[0])
            elif m == "getAccountInfo": out.append({"value": {"owner": S.TOKEN, "data": {"parsed": {"info": {"decimals": 6, "supply": str(SUPPLY), "mintAuthority": None, "freezeAuthority": None}}}}})
            elif m == "getMultipleAccounts":
                if (p[1] or {}).get("encoding") == "jsonParsed":
                    out.append({"value": [{"owner": S.TOKEN, "data": {"parsed": {"info": {"owner": W, "mint": MINT}}}} for _ in p[0]]})
                else:
                    out.append({"value": [({"owner": S.PUMP} if a == CURVE else None) for a in p[0]]})
            elif m == "getSignaturesForAddress":
                rows = sorted(({"signature": s, "slot": t["slot"], "blockTime": t["blockTime"], "err": None} for s, t in self.txs.items()), key=lambda r: -r["slot"])
                out.append(rows)
            elif m == "getTransaction": out.append(self.txs.get(p[0]))
            elif m == "getTokenLargestAccounts": out.append({"value": [{"address": "TA1", "amount": str(100_000_000 * E), "decimals": 6}]})
            else: raise AssertionError(m)
        return out

    def head(self): return {"block": 5000, "block_time": 1_700_005_000, "chain_id": "solana"}
    def remaining(self): return None
    def link(self, k, v): return f"x/{v}"


def _launch(dump=True):
    txs = {"create": ptx(100, "create", DEV, {}, {CURVE: SUPPLY - 30_000_000 * E, DEV: 30_000_000 * E})}
    txs["snipe1"] = ptx(101, "snipe1", S1, {CURVE: SUPPLY - 30_000_000 * E}, {CURVE: SUPPLY - 130_000_000 * E, S1: 100_000_000 * E}, {S1: -2_000_000_000})
    txs["snipe2"] = ptx(101, "snipe2", S2, {CURVE: SUPPLY - 130_000_000 * E}, {CURVE: SUPPLY - 230_000_000 * E, S2: 100_000_000 * E})
    txs["buy1"] = ptx(300, "buy1", W, {CURVE: SUPPLY - 230_000_000 * E}, {CURVE: SUPPLY - 231_000_000 * E, W: 1_000_000 * E})
    if dump:
        txs["sell1"] = ptx(150, "sell1", S1, {S1: 100_000_000 * E, CURVE: 1}, {S1: 0, CURVE: 100_000_000 * E + 1}, {S1: 3_000_000_000})
        txs["sell2"] = ptx(160, "sell2", S2, {S2: 100_000_000 * E, CURVE: 1}, {S2: 0, CURVE: 100_000_000 * E + 1}, {S2: 2_500_000_000})
    return txs


def test_pump_launch_dump_detected_and_clean_launch_not():
    node = FakeNode(_launch(dump=True))
    out = S.launchdump(node, MINT, blocks=1500, first_blocks=3)
    assert out["launch"]["launcher"] == DEV and out["launch"]["market"] == CURVE and out["launch"]["launchpad"] == "pump.fun"
    assert out["launch_dump"] is True and abs(out["share_bought"] - 0.20) < 1e-9  # the dev's launch allocation is a mint to the dev at balance level, not a buy and out["sold_direct"] == 200_000_000 * E
    assert out["blocks_to_first_sell"] == 50 and out["hubs"] == [] and out["coverage"]["complete"] is True
    clean = S.launchdump(FakeNode(_launch(dump=False)), MINT)
    assert clean["launch_dump"] is False and clean["dumped"] == 0


def test_solana_triage_reads_sellers_and_sol_proceeds():
    node = FakeNode(_launch(dump=True))
    out = S.triage(node, MINT, window_s=10_000)
    sellers = {s["addr"]: s for s in out["top_sellers"]}
    assert set(sellers) == {S1, S2} and sellers[S1]["sol_proceeds"] == 3.0 and sellers[S1]["pct_supply"] == 10.0
    assert out["flow"]["sellers"] == 2 and out["authorities"] == {"mint": None, "freeze": None}
    assert out["launch"]["launcher"] == DEV and out["dev_selling"] == []
    assert out["holders"][0]["owner"] == W and out["top10_wallet_pct"] == 10.0
