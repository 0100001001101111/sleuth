"""Drain investigation collector: mechanism classification (EIP-7702 sweep, allowance, key-signed), address-poisoning
quarantine, key-signed-outflow ties, suspect profiling (outbound exits, other authorities, funding via internal tx),
three-valued verdicts under truncation, partial results on budget expiry, and the single-transaction explainer."""
import threading

from sleuth.client import BudgetExceeded
from sleuth.collectors import drain
from sleuth.collectors.drain import explain_tx, investigate

V = "0x50da000000000000000000000000000000000001"
ATT = "0x02bd000000000000000000000000000000000002"       # submits the type-4 sweep, receives the USDC
DELEG = "0x7cf9000000000000000000000000000000000003"     # unverified delegate code
SPENDER = "0x6e5c000000000000000000000000000000000004"   # spends an allowance
RAILGUN = "0x1111000000000000000000000000000000000005"
RELAY = "0x2222000000000000000000000000000000000006"
DEX = "0x3333000000000000000000000000000000000007"
FUNDER = "0x4444000000000000000000000000000000000008"
POISON = "0xa0b8000000000000000000000000000000000009"    # recipient look-alike of USDC? no: a poisoner
SCAMBOT = "0x80d0000000000000000000000000000000000010"
OTHERV = "0x9999000000000000000000000000000000000011"
HOP = "0x3863000000000000000000000000000000000015"       # the address ATT rehearses with after the loss
BINANCE = "0x4976000000000000000000000000000000000016"   # tagged exchange hot wallet: HOP's first funding
NEXT = "0xd45b000000000000000000000000000000000017"
T_H1, T_H2, T_H3 = "2026-10-25T13:27:35.000000Z", "2026-10-26T07:33:11.000000Z", "2026-10-27T04:39:59.000000Z"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
FAKE_USDC = "0x3adf000000000000000000000000000000000012"
FAKE_ETH = "0x985b000000000000000000000000000000000013"
FAKE_X = "0x5555000000000000000000000000000000000014"
TX1, TX2, TX3, TX4, TX5, TX6 = ("0x" + c * 64 for c in "123456")
T_SWEEP = "2026-10-23T19:47:35.000000Z"
T_CREDIT = "2026-10-23T19:46:35.000000Z"
T_SELF = "2026-10-23T19:50:00.000000Z"
T_OLD = "2026-10-21T10:00:00.000000Z"
CFG = {"chains": {"1": {"name": "Ethereum", "symbol": "ETH", "explorer": "https://x", "canonical_tokens": {"USDC": USDC}},
                  "4663": {"name": "Robinhood Chain", "symbol": "ETH", "explorer": "https://y", "canonical_tokens": {}}}}


def tt(frm, to, raw, ts, tx, token=USDC, sym="USDC", dec="6"):
    return {"from": {"hash": frm}, "to": {"hash": to}, "timestamp": ts, "transaction_hash": tx,
            "token": {"address_hash": token, "symbol": sym, "decimals": dec, "type": "ERC-20"}, "total": {"value": str(raw), "decimals": dec}}


def tx(frm, to, wei, ts, h, typ="2", method=None, **extra):
    return {"from": {"hash": frm, **extra.pop("from_meta", {})}, "to": {"hash": to, **extra.pop("to_meta", {})}, "value": str(wei), "timestamp": ts, "hash": h,
            "status": "ok", "type": typ, "method": method, "nonce": 7, "block_number": 100, "raw_input": "0x" if method is None else "0xabcdef", **extra}


def it(frm, to, wei, ts, h):
    return {"from": {"hash": frm}, "to": {"hash": to}, "value": str(wei), "timestamp": ts, "transaction_hash": h, "success": True}


FULL = {
    TX1: tx(ATT, "0xca11bde05977b3631167028862be2a173976ca11", 0, T_SWEEP, TX1, typ="4", method="aggregate3",
            authorization_list=[{"authority": V, "address_hash": DELEG, "nonce": "5"}, {"authority": OTHERV, "address_hash": DELEG, "nonce": "1"}],
            token_transfers=[tt(V, ATT, 1000_000000, T_SWEEP, TX1), tt(OTHERV, ATT, 5_000000, T_SWEEP, TX1)]),
    TX2: tx(SPENDER, USDC, 0, T_OLD, TX2, method="transferFrom", token_transfers=[tt(V, SPENDER, 50_000000, T_OLD, TX2)]),
    TX3: tx(V, ATT, 10**15, T_SELF, TX3),
    TX4: tx(V, DEX, 5 * 10**17, "2026-10-20T10:00:00.000000Z", TX4, method="swap"),
    TX6: tx(SCAMBOT, FAKE_X, 0, T_SELF, TX6, method="transferFrom", from_meta={"is_scam": True}, token_transfers=[tt(V, POISON, 1, T_SELF, TX6, token=FAKE_X, sym="USDX")]),
}
PAGES_1 = {
    (V, "token-transfers"): [tt(V, ATT, 1000_000000, T_SWEEP, TX1), tt(FUNDER, V, 1000_000000, T_CREDIT, "0x" + "a" * 64),
                             tt(V, POISON, 1, T_SELF, "0x" + "b" * 64, token=FAKE_ETH, sym="ETH", dec="18"),
                             tt(V, ATT, 1000_000000, T_SWEEP, "0x" + "c" * 64, token=FAKE_USDC),
                             tt(V, SPENDER, 50_000000, T_OLD, TX2), tt(V, POISON, 1, T_SELF, TX6, token=FAKE_X, sym="USDX")],
    (V, "transactions"): [tx(V, ATT, 10**15, T_SELF, TX3), tx(V, DEX, 5 * 10**17, "2026-10-20T10:00:00.000000Z", TX4, method="swap")],
    (V, "internal-transactions"): [],
    (ATT, "transactions"): [tx(ATT, "0xca11bde05977b3631167028862be2a173976ca11", 0, T_SWEEP, TX1, typ="4"),
                            tx(ATT, RAILGUN, 10**18, "2026-10-24T01:00:00.000000Z", TX5, to_meta={"name": "Railgun: Relay Adapt", "is_contract": True})],
    (ATT, "token-transfers"): [tt(V, ATT, 1000_000000, T_SWEEP, TX1), tt(ATT, RAILGUN, 900_000000, "2026-10-24T01:00:00.000000Z", TX5),
                              tt(ATT, HOP, 500_000, T_H1, "0x" + "e" * 64), tt(ATT, HOP, 1_000000, T_H2, "0x" + "f" * 64),
                              tt(ATT, HOP, 1_000000, T_H2, "0x" + "9" * 64, token=FAKE_USDC)],      # a fake "USDC" to HOP: never a swept asset
    (HOP, "transactions"): [tx(HOP, ATT, 0, "2026-10-25T13:36:11.000000Z", "0x" + "1" * 64),
                            tx(BINANCE, HOP, 3596200000000000, "2026-02-05T22:35:47.000000Z", "0x" + "2" * 64,
                               from_meta={"metadata": {"tags": [{"name": "Binance: Hot Wallet"}, {"name": "Exchange"}]}})],
    (HOP, "token-transfers"): [tt(ATT, HOP, 500_000, T_H1, "0x" + "e" * 64), tt(HOP, NEXT, 1_000000, T_H3, "0x" + "3" * 64)],
    (ATT, "internal-transactions"): [it(RELAY, ATT, 2 * 10**16, "2026-10-22T00:00:00.000000Z", "0x" + "d" * 64)],
}
ADDR_1 = {
    V: {"hash": V, "is_contract": False, "proxy_type": "eip7702", "implementations": [{"address_hash": DELEG}], "coin_balance": "0"},
    DELEG: {"hash": DELEG, "is_contract": True, "is_verified": False, "name": None},
    ATT: {"hash": ATT, "is_contract": False, "coin_balance": "5"},
    RELAY: {"hash": RELAY, "is_contract": True, "name": "Railgun: Relay Adapt"},
    HOP: {"hash": HOP, "is_contract": False, "coin_balance": "1"},
}
COUNTERS_1 = {V: {"transactions_count": "2", "token_transfers_count": "6"}, ATT: {"transactions_count": "2", "token_transfers_count": "2"},
              HOP: {"transactions_count": "2", "token_transfers_count": "2"}}


class FakeClient:
    """Explorer stand-in: get/many/paginate answer from dicts; `truncate` marks address pages incomplete; `budget_after`
    raises BudgetExceeded from the n-th call on (and `many` reports it through errors like the real client)."""

    def __init__(self, chain_id, pages, addrs, counters, full, truncate=(), budget_after=None):
        self.chain_id, self.pages, self.addrs, self.counters_, self.full = str(chain_id), pages, addrs, counters, full
        self.truncate, self.budget_after = set(truncate), budget_after
        self.cfg, self.chain = CFG, CFG["chains"][str(chain_id)]
        self.explorer, self.h, self.errors, self.deadline, self.calls = self.chain["explorer"], {"concurrency": 2}, [], None, 0
        self._lock = threading.Lock()          # the collector reads in threads; the call counter must not lose increments

    def _tick(self):
        with self._lock:
            self.calls += 1
            n = self.calls
        if self.budget_after is not None and n > self.budget_after:
            raise BudgetExceeded("budget")

    def check_budget(self):
        self._tick()
        self.calls -= 1

    def remaining(self):
        return None if self.budget_after is None else max(0, self.budget_after - self.calls)

    def get(self, path, params=None, soft=False, **kw):
        self._tick()
        parts = path.strip("/").split("/")
        if parts[0] == "addresses" and len(parts) == 2:
            return self.addrs.get(parts[1].lower())
        if parts[0] == "addresses" and parts[2] == "counters":
            return self.counters_.get(parts[1].lower(), {"transactions_count": "0", "token_transfers_count": "0"})
        if parts[0] == "addresses" and parts[2] == "token-balances":
            return [{"token": {"address_hash": USDC, "symbol": "USDC", "decimals": "6"}, "value": "100000000"}] if parts[1].lower() == ATT else []
        if parts[0] == "smart-contracts":
            return {"deployed_bytecode": "0x6080604052" + "ab" * 100, **self.addrs.get("sc:" + parts[1].lower(), {})}
        if parts[0] == "transactions":
            return self.full.get(parts[1].lower())
        return None

    def many(self, paths):
        out = []
        for p in paths:
            try:
                out.append(self.get(p, soft=True))
            except BudgetExceeded:
                self.errors.append("many: BudgetExceeded")
                out.append(None)
        return out

    def paginate(self, path, max_pages=5, stop=None):
        self._tick()
        _, addr, what = path.strip("/").split("/")
        return list(self.pages.get((addr.lower(), what), [])), (addr.lower(), what) not in self.truncate

    def tx(self, h):
        self._tick()
        return self.full.get(h.lower())

    def tx_internal(self, h):
        return []


def _chains(budget_after=None, truncate=()):
    c1 = FakeClient("1", dict(PAGES_1), ADDR_1, COUNTERS_1, FULL, truncate=truncate, budget_after=budget_after)   # own dict: a test may swap a page
    c2 = FakeClient("4663", {}, {}, {}, {})          # explorer has never seen the wallet here
    return c1, {"1": c1, "4663": c2}


def test_mechanisms_quarantine_ties_and_verdicts():
    c1, chains = _chains()
    out = investigate(c1, V, incident_ts=1761248855, chains=chains)
    v = out["verdict"]
    assert v["eip7702_authorization_by_wallet_submitted_by_other"] is True
    assert v["allowance_or_permit_used_by_third_party"] is True
    assert v["wallet_key_signed_outflow"] is True
    assert v["wallet_currently_delegated"] is True
    assert v["sweep_followed_inbound_credit"] is True
    assert v["activity_on_other_chains"] is False            # 4663 never saw the address, and every presence read completed
    assert v["other_wallets_authorized_to_same_submitter"] is True
    assert v["recipient_sent_to_exchange_bridge_or_privacy_pool"] is True
    assert v["recipient_still_holds_swept_asset"] is True
    ch = out["chains"][0]
    by = {r["tx"]: r for r in ch["mechanisms"]}
    assert by[TX1]["mechanism"] == "eip7702_sweep" and by[TX1]["authorizations"][0]["delegate"] == DELEG and by[TX1]["after_credit_s"] == 60
    assert by[TX2]["mechanism"] == "allowance_used_by_spender"
    assert by[TX3]["mechanism"] == "wallet_key_signed_transfer" and by[TX3]["tie_reason"] == "recipient is a sweep party"
    assert by[TX4]["mechanism"] == "wallet_key_signed_contract_call" and not by[TX4]["tied_to_loss"]
    assert {r["tx"] for r in ch["losses"]} == {TX1, TX2, TX3}
    assert ch["loss_totals"][USDC]["raw"] == str(1050_000000)
    reasons = {q["token"]: q["reason"] for q in ch["quarantine"]}
    assert "native coin" in reasons[FAKE_ETH] and "not the USDC contract" in reasons[FAKE_USDC] and "flags the submitting address" in reasons[FAKE_X]
    assert ch["quarantine_total"] == 3 and TX6 not in by and ch["outflow_txs"] == 4      # the scam-submitter tx is not counted as an outflow
    assert out["suspects"][0]["roles"] == ["recipient", "submitter"] and out["delegations"]["4663"] == {"known": True, "delegated": False, "delegate": None}
    d = ch["current_delegation"]
    assert d["delegated"] and d["delegate"] == DELEG and d["verified"] is False and d["code_len"] == 105
    assert d["set_in_loss_tx"] == TX1 and d["set_by"] == ATT              # the live delegate is the one the sweep installed
    assert out["delegations"]["1"]["set_in_loss_tx"] == TX1                # same record, so the plain words see it too
    assert out["presence"]["4663"]["seen"] is False and out["presence"]["1"]["tx"] == 2
    s = out["suspects"][0]
    assert s["addr"] == ATT and s["other_authorities"] == 1 and s["authorities_sample"] == [OTHERV]
    assert s["swept_in"][USDC]["raw"] == str(1000_000000) and s["delegates"][0]["addr"] == DELEG   # OTHERV's 5 USDC is its own loss
    assert s["swept_assets_victim"] == [USDC] and s["loss_time_known"] is True
    assert s["holds"][USDC]["symbol"] == "USDC" and s["holds"][USDC]["decimals"] == 6      # metadata from the loss rows, not "0 raw ?"
    assert [o["kind"] for o in s["outbound"]] == ["unknown", "privacy_pool"]        # HOP (2 rehearsal sends) ranks above Railgun (1)
    assert s["outbound"][1]["native_raw"] == str(10**18)
    assert s["first_funding"]["from"] == RELAY and s["first_funding"]["via"] == "internal"
    assert out["complete"] is True and out["gaps"] == []


def test_truncated_pages_make_negatives_unknown():
    c1, chains = _chains(truncate=[(V, "transactions")])
    out = investigate(c1, V, chains=chains)
    v = out["verdict"]
    assert v["eip7702_authorization_by_wallet_submitted_by_other"] is True       # positive evidence survives truncation
    assert v["third_party_tx_unresolved"] is None                                # a negative does not
    assert out["complete"] is False and any(g["kind"] == "truncated" for g in out["gaps"])


def test_budget_expiry_keeps_partial_chain_record():
    c1, chains = _chains(budget_after=6)          # presence (4 calls) + 2 pages, then nothing
    out = investigate(c1, V, chains=chains)
    assert out["complete"] is False and any(g["kind"] == "budget" for g in out["gaps"])
    assert out["presence"]["1"]["tx"] == 2
    assert out["verdict"]["eip7702_authorization_by_wallet_submitted_by_other"] is None
    assert out["suspects"] == []
    # one page read hit the budget inside the fan-out: the chain record is kept with the pages its siblings got, not dropped
    assert len(out["chains"]) == 1 and out["chains"][0]["pages_complete"] is False and out["verdict"]["assets_left_wallet"] is not False


def test_presence_unknown_when_explorer_fails():
    class Broken(FakeClient):
        def get(self, path, params=None, soft=False, **kw):
            if path.rstrip("/").endswith(V):
                self.errors.append("GET addresses: 502")
                raise OSError("explorer down")
            return super().get(path, params, soft, **kw)
    c1 = Broken("1", PAGES_1, ADDR_1, COUNTERS_1, FULL)
    out = investigate(c1, V, chains={"1": c1})
    assert out["presence"]["1"]["tx"] is None and out["presence"]["1"]["seen"] is None
    assert out["verdict"]["wallet_currently_delegated"] is None and out["verdict"]["assets_left_wallet"] is None
    assert out["chains"] == [] and any(g["kind"] == "provider" for g in out["gaps"]) and not out["complete"]


def test_suspect_ranking_prefers_sweep_over_dust():
    cands = [(SCAMBOT, "1", 1)] * 10 + [(ATT, "1", 10)] * 2
    counts, weight = {}, {}
    for a, cid, w in cands:
        counts[(a, cid)] = counts.get((a, cid), 0) + 1
        weight[(a, cid)] = weight.get((a, cid), 0) + w
    assert min(counts, key=lambda k: (-weight[k], -counts[k])) == (ATT, "1")
    assert drain._WEIGHT["eip7702_sweep"] > 5 * drain._WEIGHT["third_party_tx_unresolved"]


def test_explain_tx_found_and_not_found():
    c1, _ = _chains()
    ex = explain_tx(c1, TX1)
    assert ex["found"] and ex["type"] == "4" and ex["submitter"] == ATT
    assert [a["authority"] for a in ex["authorizations"]] == [V, OTHERV]
    acc = {a["account"]: a for a in ex["accounts"]}
    assert acc[V]["mechanism"] == "eip7702_sweep" and acc[OTHERV]["mechanism"] == "eip7702_sweep"
    assert all(t["canonical"] and not t["suspicious"] for t in ex["token_transfers"])
    ex2 = explain_tx(c1, "0x" + "f" * 64)
    assert not ex2["found"] and ex2["gaps"][0]["kind"] == "not_found"
    ex3 = explain_tx(c1, TX6)
    assert ex3["accounts"][0]["mechanism"] == "allowance_used_by_spender" or ex3["accounts"][0]["mechanism"] == "third_party_tx_unresolved"
    assert ex3["token_transfers"][0]["suspicious"] is False or ex3["token_transfers"][0]["canonical"] is False


LOOK = "0x02bd" + "f" * 32 + "0002"                  # look-alike of ATT (same first and last characters)
TX7, TX8 = "0x" + "7" * 64, "0x" + "8" * 64
T_PAY = "2026-10-22T09:00:00.000000Z"


def _chains_with(extra_tts=(), extra_txs=(), extra_full=None, c2=None, **kw):
    pages = {k: list(v) for k, v in PAGES_1.items()}
    pages[(V, "token-transfers")] += list(extra_tts)
    pages[(V, "transactions")] += list(extra_txs)
    c1 = FakeClient("1", pages, ADDR_1, COUNTERS_1, {**FULL, **(extra_full or {})}, **kw)
    c2 = c2 or FakeClient("4663", {}, {}, {}, {})
    return c1, {"1": c1, "4663": c2}


def test_exact_approval_then_pull_is_self_authorised_not_loss():
    """approve exactly N to a router, then the router pulls exactly N five minutes later: a swap the owner ordered.
    The row is listed under third_party_self_authorised, left out of losses and totals, and does not drive the
    allowance verdict; an unlimited approval would not excuse anything."""
    t_ap, t_pull = "2026-10-22T09:00:00.000000Z", "2026-10-22T09:05:00.000000Z"
    pull, fee = tt(V, DEX, 69_900000, t_pull, TX8), tt(V, FUNDER, 100000, t_pull, TX8)    # output + fee: two rows of one order
    approve = tx(V, USDC, 0, t_ap, TX7, method="approve",
                 decoded_input={"method_call": "approve(address spender, uint256 value)",
                                "parameters": [{"name": "spender", "value": DEX}, {"name": "value", "value": "70000000"}]})
    c1, chains = _chains_with(extra_tts=[pull, fee], extra_txs=[approve],
                              extra_full={TX7: approve, TX8: tx(DEX, USDC, 0, t_pull, TX8, method="transferFrom", token_transfers=[pull, fee])})
    out = investigate(c1, V, incident_ts=drain._ts(T_SWEEP), chains=chains)
    ch = out["chains"][0]
    by = {r["tx"]: r for r in ch["mechanisms"]}
    assert by[TX8]["mechanism"] == "allowance_used_by_spender" and by[TX8]["self_authorised"] is True
    assert by[TX8]["before_reported_incident"] is True and by[TX2]["before_reported_incident"] is True and "before_reported_incident" not in by[TX1]
    assert "5 min after this wallet's own approval" in by[TX8]["self_authorised_reason"]
    assert TX8 not in {r["tx"] for r in ch["losses"]} and ch["loss_totals"][USDC]["raw"] == str(1000_000000)
    assert [r["tx"] for r in ch["third_party_self_authorised"]] == [TX8]
    # TX2 (an allowance pull two days before the reported time) is earlier history: listed with its own total, not the loss
    assert [r["tx"] for r in ch["earlier_third_party"]] == [TX2] and ch["earlier_totals"][USDC]["raw"] == str(50_000000)
    assert TX2 not in {r["tx"] for r in ch["losses"]}
    assert out["verdict"]["allowance_or_permit_used_by_third_party"] is False    # nothing in the reported window used an allowance


def test_canonical_outflow_to_lookalike_is_a_loss_not_quarantine():
    """The poisoning payoff: the wallet sends a REAL asset to an address that looks like a real counterparty. That is the
    loss itself, so it stays an outflow, is tied to the loss and is summed; only incoming / fake-token look-alike rows
    are set aside."""
    pay = tt(V, LOOK, 20_000000, T_PAY, TX7)
    c1, chains = _chains_with(extra_tts=[pay], extra_txs=[tx(V, USDC, 0, T_PAY, TX7, method="transfer")],
                              extra_full={TX7: tx(V, USDC, 0, T_PAY, TX7, method="transfer", token_transfers=[pay])})
    out = investigate(c1, V, incident_ts=1761248855, chains=chains)
    ch = out["chains"][0]
    by = {r["tx"]: r for r in ch["mechanisms"]}
    assert by[TX7]["mechanism"] == "wallet_key_signed_transfer" and by[TX7]["tied_to_loss"] and "look-alike" in by[TX7]["tie_reason"]
    assert TX7 in {r["tx"] for r in ch["losses"]} and ch["loss_totals"][USDC]["raw"] == str(1070_000000)
    assert all(q["tx"] != TX7 for q in ch["quarantine"]) and by[TX7]["outflows"][0]["lookalike_recipient"] is True


def test_self_signed_transfer_to_labelled_service_is_not_tied():
    """A key-signed send to a router/pool the sweep also touched is a shared service, not a tie (shared service != owner)."""
    full = dict(FULL)
    full[TX1] = {**FULL[TX1], "to": {"hash": DEX, "name": "Uniswap V3: Router"},
                 "token_transfers": [tt(V, DEX, 1000_000000, T_SWEEP, TX1), tt(OTHERV, ATT, 5_000000, T_SWEEP, TX1)]}
    pages = {k: list(v) for k, v in PAGES_1.items()}
    pages[(V, "token-transfers")][0] = tt(V, DEX, 1000_000000, T_SWEEP, TX1)
    pages[(V, "transactions")] = [{**tx(V, DEX, 5 * 10**17, "2026-10-20T10:00:00.000000Z", TX4, method="swap"), "to": {"hash": DEX, "name": "Uniswap V3: Router"}}]
    full[TX4] = {**FULL[TX4], "to": {"hash": DEX, "name": "Uniswap V3: Router"}}
    c1 = FakeClient("1", pages, ADDR_1, COUNTERS_1, full)
    out = investigate(c1, V, incident_ts=1761248855, chains={"1": c1, "4663": FakeClient("4663", {}, {}, {}, {})})
    by = {r["tx"]: r for r in out["chains"][0]["mechanisms"]}
    assert by[TX1]["mechanism"] == "eip7702_sweep" and DEX in by[TX1]["recipients"]
    assert by[TX4]["mechanism"] == "wallet_key_signed_contract_call" and not by[TX4]["tied_to_loss"]     # labelled router: shared service


def test_delegation_found_on_a_quiet_chain():
    """A delegated account with zero counters and no balance on another chain is still an open door: the delegation check
    covers every chain the explorer knows the address on, not only the chains busy enough to be read."""
    addr1 = {**ADDR_1, V: {"hash": V, "is_contract": False, "coin_balance": "0"}}            # no delegation on chain 1
    c1 = FakeClient("1", PAGES_1, addr1, COUNTERS_1, FULL)
    c2 = FakeClient("4663", {}, {V: {"hash": V, "proxy_type": "eip7702", "implementations": [{"address_hash": DELEG}], "coin_balance": "0"}, DELEG: ADDR_1[DELEG]},
                    {V: {"transactions_count": "0", "token_transfers_count": "0"}}, {})
    out = investigate(c1, V, chains={"1": c1, "4663": c2})
    assert out["chains"][0]["chain_id"] == "1" and len(out["chains"]) == 1                 # 4663 was not active, so not read
    assert out["delegations"]["4663"]["delegated"] is True and out["delegations"]["1"]["delegated"] is False
    assert out["verdict"]["wallet_currently_delegated"] is True


def test_capped_chain_turns_negatives_unknown():
    """With activity on more chains than are read, "no" is withheld for everything the unread chain could have changed."""
    c2 = FakeClient("4663", {(V, "transactions"): [tx(V, DEX, 10**17, T_OLD, "0x" + "e" * 64)], (V, "token-transfers"): [], (V, "internal-transactions"): []},
                    {V: {"hash": V, "coin_balance": "1"}}, {V: {"transactions_count": "1", "token_transfers_count": "0"}}, {})
    c1, chains = _chains_with(c2=c2)
    out = investigate(c1, V, incident_ts=1761248855, chains=chains, max_chains=1)
    v = out["verdict"]
    assert [c["chain_id"] for c in out["chains"]] == ["1"] and any(g["kind"] == "capped" and "busiest" in g["detail"] for g in out["gaps"])
    assert v["activity_on_other_chains"] is True and v["eip7702_authorization_by_wallet_submitted_by_other"] is True
    assert v["third_party_tx_unresolved"] is None and v["assets_left_wallet"] is True and out["complete"] is False


def test_explain_tx_provider_failure_is_not_not_found():
    class Flaky(FakeClient):
        def tx(self, h):
            self.errors.append("GET transactions: 502")

        def tx_internal(self, h):
            self.errors.append("GET internal: 502")
            return []
    c = Flaky("1", PAGES_1, ADDR_1, COUNTERS_1, FULL)
    ex = explain_tx(c, TX1)
    assert not ex["found"] and ex["gaps"][0]["kind"] == "provider"

    class FlakyInternal(FakeClient):
        def tx_internal(self, h):
            self.errors.append("GET internal: 502")
            return []
    ex2 = explain_tx(FlakyInternal("1", PAGES_1, ADDR_1, COUNTERS_1, FULL), TX1)
    assert ex2["found"] and ex2["complete"] is False and any(g["kind"] == "provider" for g in ex2["gaps"])


def test_explain_tx_authorization_only_account_and_reverted_value():
    """An account that signed an authorization but lost nothing in the tx is listed, not counted; a reverted call moves no value."""
    full = {TX8: tx(ATT, "0xca11bde05977b3631167028862be2a173976ca11", 10**18, T_SWEEP, TX8, typ="4", method="aggregate3",
                    authorization_list=[{"authority": OTHERV, "address_hash": DELEG, "nonce": "1"}],
                    token_transfers=[tt(V, ATT, 1000_000000, T_SWEEP, TX8)], decoded_input="garbage")}
    full[TX8]["status"] = "error"
    c = FakeClient("1", PAGES_1, ADDR_1, COUNTERS_1, full)
    ex = explain_tx(c, TX8)
    acc = {a["account"]: a for a in ex["accounts"]}
    assert acc[V]["assets_left"] is True and acc[OTHERV]["assets_left"] is False and acc[OTHERV]["mechanism"] == "eip7702_sweep"
    assert ex["native_transfers"] == [] and ex["method"] == "aggregate3"


def test_canonical_outflow_to_scam_flagged_recipient_is_a_loss_not_quarantine():
    """A real asset the wallet sent to an address the explorer flags as scam is the loss, not noise: the scam flag
    only quarantines INCOMING rows (fake-token airdrops, dust from flagged senders)."""
    pay = tt(V, SCAMBOT, 30_000000, T_PAY, TX7)
    pay["to"]["is_scam"] = True
    c1, chains = _chains_with(extra_tts=[pay], extra_txs=[tx(V, USDC, 0, T_PAY, TX7, method="transfer")],
                              extra_full={TX7: tx(V, USDC, 0, T_PAY, TX7, method="transfer", token_transfers=[pay])})
    out = investigate(c1, V, incident_ts=1761248855, chains=chains)
    ch = out["chains"][0]
    by = {r["tx"]: r for r in ch["mechanisms"]}
    assert by[TX7]["mechanism"] == "wallet_key_signed_transfer" and by[TX7]["outflows"][0]["raw"] == str(30_000000)
    assert all(q["tx"] != TX7 for q in ch["quarantine"])
    assert any(q["tx"] == "0x" + "b" * 64 for q in ch["quarantine"])            # the incoming fake-ETH row is still set aside


def test_internal_native_send_to_lookalike_is_flagged():
    """Native coin leaving through a contract call (internal transfer) to a look-alike of a real counterparty is the
    poisoning payoff just like a token transfer would be."""
    pages = {k: list(v) for k, v in PAGES_1.items()}
    pages[(V, "internal-transactions")] = [it(V, LOOK, 10**16, T_PAY, TX7)]
    pages[(V, "transactions")] += [tx(V, DEX, 0, T_PAY, TX7, method="execute")]
    c1 = FakeClient("1", pages, ADDR_1, COUNTERS_1, {**FULL, TX7: tx(V, DEX, 0, T_PAY, TX7, method="execute")})
    out = investigate(c1, V, incident_ts=1761248855, chains={"1": c1, "4663": FakeClient("4663", {}, {}, {}, {})})
    rows = [o for r in out["chains"][0]["mechanisms"] if r["tx"] == TX7 for o in r["outflows"]]
    assert rows and rows[0]["via"] == "internal" and rows[0]["lookalike_recipient"] is True


def test_first_page_provider_failure_is_a_provider_gap():
    class Flaky(FakeClient):
        def paginate(self, path, max_pages=1, stop=None):
            if "token-transfers" in path and path.startswith(f"/addresses/{V}"):
                self.errors.append("GET token-transfers: 502")
                return [], False
            return super().paginate(path, max_pages=max_pages, stop=stop)
    c1 = Flaky("1", PAGES_1, ADDR_1, COUNTERS_1, FULL)
    out = investigate(c1, V, incident_ts=1761248855, chains={"1": c1, "4663": FakeClient("4663", {}, {}, {}, {})})
    kinds = {(g["kind"], g["chain_id"]) for g in out["gaps"] if "token-transfers" in g["detail"]}
    assert ("provider", "1") in kinds and ("truncated", "1") not in kinds
    assert out["verdict"]["assets_left_wallet"] is not False


def test_next_hop_profiled_back_to_exchange_funding_and_rehearsal():
    """The recipient paid HOP two tiny amounts of the swept asset after the loss (rehearsal), HOP answered with a
    transaction, HOP was first funded by a tagged exchange hot wallet and passed USDC on. All of it stated three-valued;
    the fake 'USDC' row is matched by contract, so it never counts."""
    c1, chains = _chains()
    out = drain.investigate(c1, V, chains=chains)
    att = next(s for s in out["suspects"] if s["addr"] == ATT)
    row = next(r for r in att["outbound"] if r["addr"] == HOP)
    assert [t["raw"] for t in row["transfers"]] == ["500000", "1000000"] and row["rehearsal"] is True
    assert att["next_hop_candidates"] == [HOP]                      # RAILGUN is a privacy pool: a sink, not a hop
    hop = next(h for h in out["next_hops"] if h["addr"] == HOP)
    assert hop["origin"] == ATT and hop["rehearsal"] is True and hop["complete"] is True
    assert hop["first_funding"]["kind"] == "exchange" and "Binance" in hop["first_funding"]["label"]
    assert hop["first_funding"]["earliest_page_only"] is False
    assert hop["contact_back"]["ts"].startswith("2026-10-25T13:36")
    assert [s["kind"] for s in hop["services"]] == ["exchange"] and hop["services"][0]["direction"] == ["in"]
    assert hop["forwarded"][0]["to"] == NEXT and hop["forwarded"][0]["raw"] == "1000000"
    v = out["verdict"]
    assert v["recipient_rehearsal_sends"] is True
    assert v["next_hop_first_funded_by_exchange"] is True and v["next_hop_touched_exchange"] is True


def test_next_hop_negatives_unknown_when_pages_truncated():
    c1, chains = _chains(truncate=((HOP, "transactions"),))
    out = drain.investigate(c1, V, chains=chains)
    hop = next(h for h in out["next_hops"] if h["addr"] == HOP)
    assert hop["complete"] is False and hop["first_funding"]["earliest_page_only"] is True
    assert out["verdict"]["next_hop_first_funded_by_exchange"] is None      # the oldest page was not reached: an older funder could exist
    assert out["verdict"]["next_hop_touched_exchange"] is True
    c1, chains = _chains()
    c1.pages[(HOP, "transactions")] = [tx(FUNDER, HOP, 10**15, "2026-02-05T22:35:47.000000Z", "0x" + "4" * 64)]
    c1.truncate.add((HOP, "transactions"))
    out = drain.investigate(c1, V, chains=chains)
    assert out["verdict"]["next_hop_first_funded_by_exchange"] is None and out["verdict"]["next_hop_touched_exchange"] is None


def test_bundler_submitter_is_a_service_not_a_suspect():
    """An ERC-4337 bundler submitted the sweep: it is labelled a service, not profiled, and answers no suspect question."""
    addrs = {**ADDR_1, ATT: {"hash": ATT, "is_contract": False, "coin_balance": "5", "metadata": {"tags": [{"name": "Bundler: 0x02b...002"}]}}}
    c1 = FakeClient("1", PAGES_1, addrs, COUNTERS_1, FULL)
    out = drain.investigate(c1, V, chains={"1": c1, "4663": FakeClient("4663", {}, {}, {}, {})})
    att = next(s for s in out["suspects"] if s["addr"] == ATT)
    assert att["kind"] == "bundler" and att["service"] is True and att["complete"] is True and att["outbound"] == []
    assert "not a party" in att["notes"][0]
    assert out["verdict"]["other_wallets_authorized_to_same_submitter"] is False   # answered by SPENDER alone, the only real submitter
    assert out["delegations"]["1"]["set_by_kind"] == "bundler"
    assert out["next_hops"] == []                                    # a service is never followed


def test_verified_delegate_hardcoded_receiver_is_named():
    rec = "0x14f9000000000000000000000000000000000018"
    addrs = {**ADDR_1, DELEG: {"hash": DELEG, "is_contract": True, "is_verified": True, "name": "EIP7702Delegate", "creator_address_hash": rec},
             "sc:" + DELEG: {"is_verified": True, "source_code": "contract EIP7702Delegate { address public constant RECEIVER = 0x14F9000000000000000000000000000000000018;\n"
                                            "address public constant ZERO = 0x0000000000000000000000000000000000000000; }"}}
    c1 = FakeClient("1", PAGES_1, addrs, COUNTERS_1, FULL)
    out = drain.investigate(c1, V, chains={"1": c1, "4663": FakeClient("4663", {}, {}, {}, {})})
    d = out["delegations"]["1"]
    assert d["creator"] == rec and d["forwards_to"] == [{"name": "RECEIVER", "addr": rec, "is_creator": True}]
    assert drain._hardcoded_addresses(None) == [] and drain._hardcoded_addresses("no constants here") == []


DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"


class _SwappedClient(FakeClient):
    """ATT holds DAI (the swap output), no USDC left: balances come from the explorer, so the fake must say so."""

    def get(self, path, params=None, soft=False, **kw):
        if "token-balances" in path and path.split("/")[2].lower() == ATT:
            return [{"token": {"address_hash": DAI, "symbol": "DAI", "decimals": "18"}, "value": str(98_000 * 10**18)}]
        return super().get(path, params, soft, **kw)


def test_swap_output_is_followed_like_the_swept_asset():
    """ATT swapped the swept USDC into DAI in one transaction, then sent HOP 1 DAI and 10 DAI. DAI never left the victim,
    but it is the loot in a new coat: the rehearsal is detected on it and HOP is profiled. This is the exact shape of
    the real case the first automated pass missed; a symbol-only match on a fake 'DAI' row never counts."""
    cfg = {"chains": {**CFG["chains"], "1": {**CFG["chains"]["1"], "canonical_tokens": {"USDC": USDC, "DAI": DAI}}}}
    swap = "0x" + "a" * 64
    pages = {k: list(v) for k, v in PAGES_1.items()}
    pages[(ATT, "token-transfers")] = [tt(V, ATT, 1000_000000, T_SWEEP, TX1),
                                      tt(ATT, DEX, 999_000000, "2026-10-24T01:00:00.000000Z", swap), tt(DEX, ATT, 998 * 10**18, "2026-10-24T01:00:00.000000Z", swap, token=DAI, sym="DAI", dec="18"),
                                      tt(ATT, HOP, 10**18, T_H1, "0x" + "e" * 64, token=DAI, sym="DAI", dec="18"),
                                      tt(ATT, HOP, 10 * 10**18, T_H2, "0x" + "f" * 64, token=DAI, sym="DAI", dec="18"),
                                      tt(ATT, NEXT, 10**18, T_H2, "0x" + "9" * 64, token=FAKE_USDC, sym="DAI", dec="18")]   # fake "DAI": not followed
    pages[(HOP, "token-transfers")] = [tt(ATT, HOP, 10**18, T_H1, "0x" + "e" * 64, token=DAI, sym="DAI", dec="18"),
                                      tt(HOP, NEXT, 10 * 10**18, T_H3, "0x" + "b" * 64, token=DAI, sym="DAI", dec="18")]
    c1 = _SwappedClient("1", pages, ADDR_1, COUNTERS_1, FULL)
    c1.cfg, c1.chain = cfg, cfg["chains"]["1"]
    out = drain.investigate(c1, V, chains={"1": c1, "4663": FakeClient("4663", {}, {}, {}, {})})
    att = next(s for s in out["suspects"] if s["addr"] == ATT)
    assert list(att["converted"]) == [DAI] and att["holds"][DAI]["raw"] == str(98_000 * 10**18)
    row = next(r for r in att["outbound"] if r["addr"] == HOP)
    assert [t["symbol"] for t in row["transfers"]] == ["DAI", "DAI"] and row["rehearsal"] is True
    assert att["next_hop_candidates"] == [HOP]                     # the fake-"DAI" destination is not a candidate
    hop = next(h for h in out["next_hops"] if h["addr"] == HOP)
    assert hop["forwarded"][0]["to"] == NEXT and hop["forwarded"][0]["raw"] == str(10 * 10**18)
    assert out["verdict"]["recipient_rehearsal_sends"] is True


def test_zero_value_rows_are_not_rehearsal_sends_or_hops():
    """Two zero-value DAI rows from ATT to a second address (the shape of a spam or poisoning transfer) have the
    repetition of a rehearsal but move nothing: they are neither rehearsal sends nor a next-hop candidate, and a
    zero-value row from HOP is not a forward. The real second live run counted three such rows as a hop."""
    cfg = {"chains": {**CFG["chains"], "1": {**CFG["chains"]["1"], "canonical_tokens": {"USDC": USDC, "DAI": DAI}}}}
    zero_dest = "0x38630e0000000000000000000000000000000018"
    swap = "0x" + "a" * 64
    pages = {k: list(v) for k, v in PAGES_1.items()}
    pages[(ATT, "token-transfers")] = [tt(V, ATT, 1000_000000, T_SWEEP, TX1),
                                      tt(ATT, DEX, 999_000000, "2026-10-24T01:00:00.000000Z", swap), tt(DEX, ATT, 998 * 10**18, "2026-10-24T01:00:00.000000Z", swap, token=DAI, sym="DAI", dec="18"),
                                      tt(ATT, HOP, 10**18, T_H1, "0x" + "e" * 64, token=DAI, sym="DAI", dec="18"),
                                      tt(ATT, HOP, 10 * 10**18, T_H2, "0x" + "f" * 64, token=DAI, sym="DAI", dec="18"),
                                      tt(ATT, zero_dest, 0, T_H1, "0x" + "7" * 64, token=DAI, sym="DAI", dec="18"),
                                      tt(ATT, zero_dest, 0, T_H2, "0x" + "8" * 64, token=DAI, sym="DAI", dec="18")]
    pages[(HOP, "token-transfers")] = [tt(ATT, HOP, 10**18, T_H1, "0x" + "e" * 64, token=DAI, sym="DAI", dec="18"),
                                      tt(HOP, NEXT, 0, T_H3, "0x" + "b" * 64, token=DAI, sym="DAI", dec="18")]
    c1 = _SwappedClient("1", pages, ADDR_1, COUNTERS_1, FULL)
    c1.cfg, c1.chain = cfg, cfg["chains"]["1"]
    out = drain.investigate(c1, V, chains={"1": c1, "4663": FakeClient("4663", {}, {}, {}, {})})
    att = next(s for s in out["suspects"] if s["addr"] == ATT)
    zero = next(r for r in att["outbound"] if r["addr"] == zero_dest)
    assert zero["transfers"] == [] and zero["rehearsal"] is False and zero["n"] == 2       # still listed as a destination, never a lead
    assert att["next_hop_candidates"] == [HOP]
    hop = next(h for h in out["next_hops"] if h["addr"] == HOP)
    assert hop["forwarded"] == []


def test_later_suspect_pair_yields_to_the_hop_reserve():
    """With the budget nearly spent after the first suspect pair and a next hop already found, the second pair is
    skipped as unknown (a budget gap, suspects_unprofiled) so the hop walk, the one read that can reach an exchange,
    still runs. Four loss recipients are forced by giving the sweep four token credits."""
    full = dict(FULL)
    extra = ["0x7700000000000000000000000000000000000021", "0x7700000000000000000000000000000000000022", "0x7700000000000000000000000000000000000023"]
    full[TX1] = {**FULL[TX1], "token_transfers": FULL[TX1]["token_transfers"] + [tt(V, e, 1_000000, T_SWEEP, TX1) for e in extra]}
    pages = {k: list(v) for k, v in PAGES_1.items()}
    pages[(V, "token-transfers")] = PAGES_1[(V, "token-transfers")] + [tt(V, e, 1_000000, T_SWEEP, TX1) for e in extra]
    c1 = FakeClient("1", pages, ADDR_1, COUNTERS_1, full)
    c1.budget_after = 10_000                 # remaining() is now a call count; the reserve below is in the same unit
    out, seen = {}, {}

    real_remaining = c1.remaining
    def tight():                            # after ATT's pair has been read, report "almost nothing left"
        return 1 if any("next_hop_candidates" in s and s.get("next_hop_candidates") for s in seen.get("suspects", [])) else real_remaining()
    c1.remaining = tight
    orig = drain._Investigation.suspect
    def spy(self, cl, a, *args, **kw):
        s = orig(self, cl, a, *args, **kw)
        seen.setdefault("suspects", []).append(s)
        return s
    drain._Investigation.suspect = spy
    try:
        out = drain.investigate(c1, V, chains={"1": c1, "4663": FakeClient("4663", {}, {}, {}, {})})
    finally:
        drain._Investigation.suspect = orig
    profiled = [s["addr"] for s in out["suspects"]]
    assert ATT in profiled and len(profiled) == 2
    assert any("reserved for the next-hop walk" in g["detail"] for g in out["gaps"])
    assert any(h["addr"] == HOP and h["complete"] for h in out["next_hops"])
    assert out["verdict"]["next_hop_first_funded_by_exchange"] is True   # positive evidence on the walked hop stands
    assert out["complete"] is False                                     # two recipients were never read: the run says so
    # with no exchange on the walked hop, the hop negatives are unknown, not False: a skipped recipient could have its own hop
    pages[(HOP, "transactions")] = [tx(FUNDER, HOP, 10**15, "2026-02-05T22:35:47.000000Z", "0x" + "4" * 64)]
    c1 = FakeClient("1", pages, ADDR_1, COUNTERS_1, full)
    c1.budget_after, seen["suspects"] = 10_000, []
    real_remaining = c1.remaining               # tight() must call the NEW client's counter, not its own wrapper
    c1.remaining = tight
    drain._Investigation.suspect = spy
    try:
        out = drain.investigate(c1, V, chains={"1": c1, "4663": FakeClient("4663", {}, {}, {}, {})})
    finally:
        drain._Investigation.suspect = orig
    assert any("reserved for the next-hop walk" in g["detail"] for g in out["gaps"])
    assert out["verdict"]["next_hop_first_funded_by_exchange"] is None and out["verdict"]["next_hop_touched_exchange"] is None
