"""Verdict-correctness hardening: fail-closed dev verdicts, role accumulation, mint-fallback launcher
identification, and log-cap splitting. Every test here guards a path that once produced a false negative."""
from sleuth import devhistory, launch, onchain
from sleuth.onchain import TRANSFER, LOG_CAP
from sleuth.triage import _dev_verdict
from sleuth.watchlist import Watchlist
from fixtures import *  # noqa: F401,F403


# --- _dev_verdict: an empty dev set must NEVER yield False (the GH0ST hollow-verdict bug) ---

def _row(addr, sold):
    return {"addr": addr, "sold": sold}


def test_dev_verdict_empty_dev_set_is_unknown_even_with_clean_coverage():
    assert _dev_verdict([_row(DEV, 0)], set(), False, True) is None
    assert _dev_verdict([], set(), False, True) is None


def test_dev_verdict_true_on_any_dev_sale_even_with_bad_coverage():
    assert _dev_verdict([_row(DEV, 5.0)], {DEV}, False, False) is True


def test_dev_verdict_false_only_when_checked_and_complete():
    assert _dev_verdict([_row(DEV, 0)], {DEV}, True, True) is False
    assert _dev_verdict([_row(DEV, 0)], {DEV}, False, True) is None   # dev outside the tracking cap
    assert _dev_verdict([_row(DEV, 0)], {DEV}, True, False) is None   # transfer logs incomplete


def test_devhistory_verdict_three_valued():
    clean = {"swap_confirmed_sells": [], "unconfirmed_venue_outflows": []}
    sold = {"swap_confirmed_sells": ["0xs"], "unconfirmed_venue_outflows": []}
    ambig = {"swap_confirmed_sells": [], "unconfirmed_venue_outflows": ["0xv"]}
    assert devhistory._verdict([sold, clean], complete=False) is True
    assert devhistory._verdict([clean], complete=True) is False
    assert devhistory._verdict([clean], complete=False) is None      # partial history
    assert devhistory._verdict([ambig], complete=True) is None       # LP add vs sell undecided


# --- watchlist: roles accumulate; a dev label can never be downgraded by a later 'seller' add ---

def test_watchlist_roles_accumulate_and_dev_is_never_downgraded():
    wl = Watchlist("4663", TOKEN)
    wl.data["addresses"] = {}
    wl.add(DEV, "initial_recipient", "received in creation tx", ["0xc"])
    wl.add(DEV, "launcher", "sent creation tx", ["0xc"])
    wl.add(DEV, "seller", "sold in window", ["0xs"])
    e = wl.data["addresses"][DEV]
    assert e["role"] == "launcher"
    assert set(e["roles"]) == {"initial_recipient", "launcher", "seller"}
    assert DEV in wl.by_role("launcher", "dev")
    assert DEV in wl.by_role("seller")


# --- launch.find_by_mint: a token with no configured launchpad still yields a dev CANDIDATE ---

class FakeMintClient:
    chain_id = "4663"
    cache = None

    def __init__(self):
        self.errors = []
        mint = lambda to, idx: {"transactionHash": "0xmint", "blockNumber": hex(500), "logIndex": hex(idx),
                                "topics": [TRANSFER, onchain.pad(ZERO), onchain.pad(to)], "data": hex(1000 * E)}
        self.logs = [mint(MARKET, 0), mint(SIBS[0], 1)]

    def rpc(self, calls, batch=40, soft=False, errors=None):
        res = []
        for method, params in calls:
            if method == "eth_getLogs":
                res.append(list(self.logs))
            elif method == "eth_getTransactionByHash":
                res.append({"from": DEV})
            elif method == "eth_getCode":
                res.append("0x6080" if params[0] == MARKET else "0x")
            else:
                res.append(None)
        return res


def test_find_by_mint_identifies_signer_as_candidate_not_proven_dev():
    li = launch.find_by_mint(FakeMintClient(), TOKEN, 1000)
    assert li["launcher"] == DEV
    assert "candidate" in li["launcher_source"]  # reports must not present this as proven
    assert li["market"] == MARKET
    assert li["creation_tx"] == "0xmint"
    assert [r["addr"] for r in li["initial_recipients"]] == [SIBS[0]]  # EOA mint recipients only


# --- transfer_logs: a result AT the 10k cap, or a cap error, must split the range, never truncate ---

class FakeCapClient:
    chain_id = "4663"
    cache = None

    def __init__(self, mode):
        self.errors = []
        self.calls = []
        self.mode = mode  # "silent" = full list at cap; "error" = None + cap error text

    def rpc(self, calls, batch=40, soft=False, errors=None):
        sink = errors if errors is not None else self.errors
        ((method, (q,)),) = calls
        start, end = int(q["fromBlock"], 16), int(q["toBlock"], 16)
        self.calls.append((start, end))
        if end - start > 50:  # "too many logs" for a wide span
            if self.mode == "error":
                sink.append("rpc eth_getLogs: query returned more than 10000 results")
                return [None]
            return [[{}] * LOG_CAP]  # silent truncation: a full page with no error
        lg = {"transactionHash": f"0x{start:x}", "blockNumber": hex(start), "logIndex": "0x0",
              "topics": [TRANSFER, onchain.pad(DEV), onchain.pad(SIBS[0])], "data": hex(E)}
        return [[lg]]


def test_transfer_logs_splits_on_silent_cap_truncation():
    cl = FakeCapClient("silent")
    logs = onchain.transfer_logs(cl, TOKEN, 0, 100)
    assert len(cl.calls) > 1                      # the wide span was split, not accepted
    assert len(logs) >= 2                         # both halves contributed
    assert logs == sorted(logs, key=lambda x: (x["block"], x["idx"]))


def test_transfer_logs_splits_on_cap_error():
    cl = FakeCapClient("error")
    logs = onchain.transfer_logs(cl, TOKEN, 0, 100)
    assert len(cl.calls) > 1 and len(logs) >= 2
    assert cl.errors == []  # the oversized query was fully recovered by splitting: no phantom coverage gap


# --- transfer_logs_batch: aligned results, None on any miss, and never a recorded error (callers fall back) ---

class FakeBatchClient:
    chain_id = "4663"
    cache = None

    def __init__(self):
        self.errors = []

    def rpc(self, calls, batch=40, soft=False, errors=None):
        res = []
        for method, params in calls:
            if method != "eth_getLogs":
                res.append("0x1237")  # the oversize placeholder sub-call
                continue
            lo = int(params[0]["fromBlock"], 16)
            if lo == 666:
                res.append(None)  # failed query
            elif lo == 777:
                res.append([{}] * LOG_CAP)  # full page: possibly silently truncated
            else:
                res.append([{"transactionHash": "0xa", "blockNumber": params[0]["fromBlock"], "logIndex": "0x0",
                             "topics": [TRANSFER, onchain.pad(DEV), onchain.pad(SIBS[0])], "data": hex(E)}])
        return res


def test_transfer_logs_batch_aligns_and_returns_none_for_every_miss():
    cl = FakeBatchClient()
    res = onchain.transfer_logs_batch(cl, TOKEN, [
        (0, 20_000_000, None, None),  # over MAX_RANGE: skipped, placeholder keeps alignment
        (666, 1000, None, DEV),       # RPC failure
        (777, 1000, DEV, None),       # page at the log cap
        (5, 1000, None, None),        # good
    ])
    assert res[:3] == [None, None, None]  # every miss is a None the caller must re-fetch authoritatively
    assert [x["from"] for x in res[3]] == [DEV]
    assert cl.errors == []  # soft: a batch miss is never an evidence gap of its own


# --- block-timestamp cache: exact values only, height verified, misses never cached ---

class DictCache:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k), k in self.d

    def put(self, k, v, chain_id, block):
        self.d[k] = v


class FakeTsClient:
    chain_id = "4663"

    def __init__(self):
        self.errors = []
        self.cache = DictCache()
        self.calls = 0

    def rpc(self, calls, batch=40, soft=False, errors=None):
        out = []
        for _m, (b, _full) in calls:
            self.calls += 1
            n = int(b, 16)
            if n == 13:
                out.append({"number": hex(99), "timestamp": hex(1)})  # provider echoes the WRONG height
            elif n == 14:
                out.append(None)  # failed sub-call
            else:
                out.append({"number": b, "timestamp": hex(1000 + n)})
        return out


def test_block_timestamp_cache_verifies_height_and_never_caches_a_miss():
    cl = FakeTsClient()
    ts = onchain._ts_fetch(cl, [7, 13, 14])
    assert ts[7] == 1007
    assert ts[13] is None and ts[14] is None  # mismatched height / failure yield None, never a wrong timestamp
    assert cl.calls == 3
    ts2 = onchain._ts_fetch(cl, [7, 13])
    assert ts2[7] == 1007 and cl.calls == 4  # 7 came from cache; the unverifiable 13 was re-fetched, not cached


# --- swap_evidence: an RPC failure must land the venue in `unresolved`, never in silent 'no evidence' ---

class FakeSwapClient:
    def __init__(self, answers):
        self.answers = answers  # addr -> list (logs) | None (RPC failure)
        self.errors = []

    def rpc(self, calls, batch=40, soft=False, errors=None):
        return [self.answers.get(q["address"]) for _, (q,) in calls]


def test_swap_evidence_is_fail_closed():
    pool, sink, broken = POOL, BURNER, MARKET
    spans = {a: (100, 200) for a in (pool, sink, broken)}
    conf, unres = onchain.swap_evidence(FakeSwapClient({pool: [{"x": 1}], sink: [], broken: None}),
                                        [pool, sink, broken], spans, ["0xtopic"])
    assert conf == {pool}
    assert unres == {broken}          # failure is unresolved, NOT 'no evidence'
    assert sink not in conf and sink not in unres  # a clean empty answer is real evidence of a sink
    # no configured swap topics: nothing can be confirmed, everything is unresolved
    conf, unres = onchain.swap_evidence(FakeSwapClient({}), [pool], spans, [])
    assert conf == set() and unres == {pool}


def test_swap_evidence_chunks_wide_spans_and_never_clears_a_sampled_span():
    class Recorder(FakeSwapClient):
        calls = ()

        def rpc(self, calls, batch=40, soft=False, errors=None):
            self.calls = list(self.calls) + [(int(q["fromBlock"], 16), int(q["toBlock"], 16)) for _, (q,) in calls]
            return [[] for _ in calls]
    cl = Recorder({})
    conf, unres = onchain.swap_evidence(cl, [POOL], {POOL: (0, 1_000_000)}, ["0xtopic"], chunk=99_000)
    assert cl.calls[:2] == [(0, 99_000), (901_000, 1_000_000)]  # first + last activity windows, under the node cap
    # the two windows only SAMPLE a 1M-block span: clean empties must NOT resolve the venue as a sink --
    # a swap in the omitted middle would otherwise become a false 'no evidence'
    assert conf == set() and unres == {POOL}
    # a span fully covered by the windows CAN be cleared by clean empties
    conf, unres = onchain.swap_evidence(Recorder({}), [POOL], {POOL: (0, 150_000)}, ["0xtopic"], chunk=99_000)
    assert conf == set() and unres == set()
