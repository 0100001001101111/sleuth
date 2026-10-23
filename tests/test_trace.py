"""Attribution rules: batch-funded siblings are coordinated, a shared *service* is not shared funding, dev links are separate."""
from sleuth import trace
from fixtures import *  # noqa: F401,F403


class FakeClient:
    """Serves canned explorer pages and RPC logs for the four-wallet STOX sniper group."""
    chain_id = "4663"
    cache = None
    cfg = {"known_services": {RELAY: "Relay bridge solver"}, "service_name_markers": ["batchtransfer", "router"], "service_tx_threshold": 20000}
    chain = {"tz": "UTC"}
    requests, errors = 0, []

    def head(self):
        return {"chain_id": 4663, "block": 93200000, "block_time": 1760930000, "time": 0}

    def rpc(self, calls, batch=40, soft=False, errors=None):
        out = []
        for m, p in calls:
            if m == "eth_getLogs":
                tp = p[0]["topics"]
                recs = []
                for sib in SIBS:  # every sibling bought from the pool in BUY_BLOCK, later sold via router
                    recs += [{"tx": f"0xbuy{sib[-4:]}", "blk": BUY_BLOCK, "f": POOL, "t": sib, "v": 48_400_000 * E},
                             {"tx": f"0xsell{sib[-4:]}", "blk": BUY_BLOCK + 500, "f": sib, "t": ROUTER, "v": 48_400_000 * E}]
                want_from = tp[1] and "0x" + tp[1][-40:]
                want_to = len(tp) > 2 and tp[2] and "0x" + tp[2][-40:]
                out.append([{"transactionHash": r["tx"], "blockNumber": hex(r["blk"]), "logIndex": "0x0", "data": hex(r["v"]),
                             "topics": [tp[0], "0x" + r["f"][2:].rjust(64, "0"), "0x" + r["t"][2:].rjust(64, "0")]}
                            for r in recs if (not want_from or r["f"] == want_from) and (not want_to or r["t"] == want_to)])
            elif m == "eth_getTransactionReceipt":
                h = p[0]
                sib = next(s for s in SIBS if s.endswith(h[-4:]))
                if h.startswith("0xbuy"):
                    logs = [(POOL, sib)]
                else:  # routed sell: wallet -> router -> pool
                    logs = [(sib, ROUTER), (ROUTER, POOL)]
                out.append({"blockNumber": hex(BUY_BLOCK if h.startswith("0xbuy") else BUY_BLOCK + 500), "logs": [
                    {"address": TOKEN, "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                                                  "0x" + f[2:].rjust(64, "0"), "0x" + t[2:].rjust(64, "0")], "data": hex(48_400_000 * E), "logIndex": hex(i)}
                    for i, (f, t) in enumerate(logs)]})
            elif m == "eth_getBlockByNumber":
                out.append({"timestamp": hex(1792460000 + int(p[0], 16) - BUY_BLOCK)})  # 2026-10-20 01:33Z, after the batch
            elif m == "eth_getCode":
                out.append("0x6080" if p[0] in (POOL, ROUTER, BATCH) else "0x")
            else:
                out.append(None)
        return out

    def address(self, a, **k):
        a = a.lower()
        names = {BATCH: "BatchTransfer", ROUTER: "RouteRacer", RELAY: None}
        return {"hash": a, "is_contract": a in (BATCH, ROUTER, POOL), "name": names.get(a), "proxy_type": None}

    def counters(self, a, **k):
        return {"tx": 2_270_000 if a.lower() == RELAY else 10, "transfers": 0}

    def paginate(self, path, max_pages=1):
        a = path.split("/")[2].lower()
        if path.endswith("/transactions"):
            if a == FUNDER:  # funder was itself funded by the Relay solver (a bridge service) and by an exchange proxy
                return [{"hash": "0xrelay", "from": {"hash": RELAY}, "to": {"hash": FUNDER}, "value": str(2 * E), "status": "ok", "timestamp": "2026-10-19T20:00:00.000000Z"}], True
            return [], True
        if path.endswith("/internal-transactions"):
            if a in SIBS:
                return [{"transaction_hash": BATCH_TX, "from": {"hash": BATCH, "name": "BatchTransfer"}, "to": {"hash": a},
                         "value": str(71483488 * 10 ** 9), "timestamp": "2026-10-20T01:02:00.000000Z", "block_number": BUY_BLOCK - 200}], True
            return [], True
        return [], True

    def tx(self, h):
        return {"from": {"hash": FUNDER}, "hash": h} if h == BATCH_TX else None

    def tx_internal(self, h):
        return [{"to": {"hash": s}, "value": str(71483488 * 10 ** 9)} for s in SIBS] if h == BATCH_TX else []


def test_batch_funded_siblings_same_block_buys_no_dev_link():
    out = trace.group(FakeClient(), SIBS, TOKEN, {DEV}, pools={POOL})
    v = out["verdict"]
    assert v["batch_funded"] and v["coordinated_trading"], out["coordinated"]
    assert out["coordinated"][0]["sender"] == FUNDER and str(BUY_BLOCK) in map(str, out["coordinated"][0]["same_block_buys"])
    assert v["dev_connection"] is False and v["ownership"].startswith("unknown")
    # BatchTransfer is a shared SERVICE, never 'shared funding'
    assert v["shared_funding"] is False and any(s["label"] == "BatchTransfer" for s in v["shared_service_only"])
    # routed sell counted once per wallet, with the pool as counterparty
    for w in out["wallets"]:
        sells = [d for d in w["token_disposals"] if d["via"] == "pool (sell)"]
        assert len(sells) == 1 and sells[0]["amount"] == 48_400_000 * E


def test_funder_in_group_is_direct_connection_and_relay_is_service_only():
    out = trace.group(FakeClient(), SIBS + [FUNDER], TOKEN, {DEV}, pools={POOL})
    v = out["verdict"]
    assert v["direct_tx_connection"], out["direct"]
    assert all(d["from"] == FUNDER for d in out["direct"])
    # The Relay solver funded the funder: a service, so not a 'shared funder' and not a dev link
    assert v["shared_funding"] is False and v["dev_connection"] is False
    labels = {e["from_label"] for w in out["wallets"] if w["addr"] == FUNDER for e in w["eth_funding"]}
    assert labels == {"Relay bridge solver"}


def test_dev_funding_is_reported_separately():
    class DevFunded(FakeClient):
        def paginate(self, path, max_pages=1):
            a = path.split("/")[2].lower()
            if path.endswith("/transactions") and a == SIBS[0]:
                return [{"hash": "0xdevfund", "from": {"hash": DEV}, "to": {"hash": a}, "value": str(E), "status": "ok", "timestamp": "2026-10-19T20:00:00.000000Z"}], True
            return super().paginate(path, max_pages)
    out = trace.group(DevFunded(), SIBS[:1], TOKEN, {DEV}, pools={POOL})
    assert out["verdict"]["dev_connection"] is True
    assert out["dev_links"][0]["type"] == "eth_from_dev" and out["dev_links"][0]["tx"] == "0xdevfund"


def test_incomplete_evidence_gives_unknown_not_no():
    class Truncated(FakeClient):
        def paginate(self, path, max_pages=1):
            items, _ = super().paginate(path, max_pages)
            return items, False  # explorer page incomplete
    out = trace.group(Truncated(), SIBS[:2], TOKEN, {DEV}, pools={POOL})
    v = out["verdict"]
    assert v["complete"] is False and v["dev_connection"] is None and v["shared_funding"] is None
    assert v["batch_funded"] is True  # positive evidence still stands
    assert any("incomplete" in g for g in out["gaps"])


def test_proceeds_inside_own_swap_are_not_funding():
    class Proceeds(FakeClient):
        def paginate(self, path, max_pages=1):
            a = path.split("/")[2].lower()
            if path.endswith("/internal-transactions") and a in SIBS:
                return [{"transaction_hash": f"0xsell{a[-4:]}", "from": {"hash": POOL}, "to": {"hash": a}, "value": str(E),
                         "timestamp": "2026-10-20T02:00:00.000000Z", "block_number": BUY_BLOCK + 500}], True
            return super().paginate(path, max_pages)
    out = trace.group(Proceeds(), SIBS[:2], TOKEN, {DEV}, pools={POOL})
    for w in out["wallets"]:
        assert w["eth_funding"] == [] and len(w["eth_in_own_token_txs"]) == 1
    assert out["verdict"]["shared_funding"] is False and out["shared_funders"] == []
