"""Offline tests for the incident service: queue leases, payment state machine + x402 crash boundaries, monitoring reorg
rollback with finalized-only alerts, and the web app end to end with a fake job runner. No RPC, no network."""
import json
import os
import sqlite3

import pytest

from service import cases, db, monitor, notify, queue, reports
from service.jobs import JOB_VERSION
from service.payments import PaymentError, base, manual, x402
from sleuth.onchain import TRANSFER, pad

VICTIM = "0x1d61c880aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN = "0xb4d3e2c0b6b3d6a8e5f4c2a1b0c9d8e7f6a5b4c3"
OTHER = "0x" + "9" * 40
ONE = "0x" + "1".rjust(64, "0")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("SLEUTH_SERVICE_DB", str(tmp_path / "svc.db"))
    monkeypatch.setenv("SLEUTH_PAY_TO_BASE_USDC", "0x" + "ab" * 20)
    c = db.connect()
    yield c
    c.close()


def _case(conn, kind="rug", subject=TOKEN):
    return cases.create(conn, kind, 4663, subject)


# ---------------------------------------------------------------- queue
def test_lease_is_exclusive_and_reclaims_expired(conn):
    _case(conn)
    run = queue.lease(conn, "w1")
    assert run and run["kind"] == "rug_screen" and run["state"] == "leased"
    assert queue.lease(conn, "w2") is None  # already leased
    conn.execute("UPDATE investigation_runs SET lease_expires_at=? WHERE id=?", (db.now() - 1, run["id"]))  # simulate crash
    again = queue.lease(conn, "w2")
    assert again and again["id"] == run["id"] and again["attempts"] == 2
    assert not queue.finish(conn, run["id"], "w1", "succeeded", result={})  # stale worker's late result is dropped
    assert queue.finish(conn, run["id"], "w2", "partial", result={"complete": False})
    assert conn.execute("SELECT state FROM investigation_runs WHERE id=?", (run["id"],)).fetchone()[0] == "partial"


def test_deep_run_invisible_until_settled(conn):
    cid, _ = _case(conn)
    queue.enqueue(conn, cid, "rug_deep", {"token": TOKEN}, JOB_VERSION, requires_entitlement=True)
    queue.finish(conn, queue.lease(conn, "w")["id"], "w", "succeeded", result={})  # consume the screen run
    assert queue.lease(conn, "w") is None
    p = manual.start(conn, cid)
    assert p["state"] == "awaiting"
    assert queue.lease(conn, "w") is None  # awaiting is worth nothing
    manual.confirm(conn, p["id"], "0x" + "cd" * 32, operator="test")
    assert queue.lease(conn, "w")["kind"] == "rug_deep"


def test_enqueue_idempotent_and_attempts_exhaust(conn):
    cid, _ = _case(conn)
    queue.finish(conn, queue.lease(conn, "w")["id"], "w", "succeeded", result={})  # consume the free screen run
    a = queue.enqueue(conn, cid, "x", {"a": 1}, "v")
    assert queue.enqueue(conn, cid, "x", {"a": 1}, "v") == a
    for i in range(queue.MAX_ATTEMPTS):
        r = queue.lease(conn, f"w{i}")
        assert r is not None
        conn.execute("UPDATE investigation_runs SET lease_expires_at=? WHERE id=?", (db.now() - 1, r["id"]))
    assert queue.lease(conn, "wx") is None
    assert queue.exhaust_stale(conn) >= 1
    states = {r[0] for r in conn.execute("SELECT state FROM investigation_runs")}
    assert "failed_terminal" in states


# ---------------------------------------------------------------- payments
def test_payment_transitions_and_duplicate_webhook(conn):
    cid, _ = _case(conn)
    p = manual.start(conn, cid)
    assert manual.start(conn, cid)["id"] == p["id"]  # idempotent checkout
    with pytest.raises(PaymentError):
        base.advance(conn, p["id"], "entitled")  # cannot skip settled
    row = manual.confirm(conn, p["id"], "0x" + "ee" * 32, operator="t")
    assert row["state"] == "entitled"
    assert manual.confirm(conn, p["id"], "0x" + "EE" * 32, operator="t")["state"] == "entitled"  # same tx twice: no-op
    with pytest.raises(PaymentError):
        manual.confirm(conn, p["id"], "0x" + "ff" * 32, operator="t")  # different hash on settled row
    cid2, _ = _case(conn, subject=OTHER)
    p2 = manual.start(conn, cid2)
    with pytest.raises(PaymentError):
        manual.confirm(conn, p2["id"], "0x" + "ee" * 32, operator="t")  # tx already credited elsewhere


def test_checkout_disabled_without_receive_address(conn, monkeypatch):
    monkeypatch.delenv("SLEUTH_PAY_TO_BASE_USDC")
    cid, _ = _case(conn)
    with pytest.raises(PaymentError):
        manual.start(conn, cid)


def test_x402_idempotent_compute_and_crash_reconcile(conn):
    fac = x402.MockFacilitator()
    req = x402.requirements("/v1/screen/token", "0x" + "ab" * 20)
    calls = []

    def compute():
        calls.append(1)
        return {"token": TOKEN, "complete": True}

    params = {"token": TOKEN, "chain_id": 4663}
    st, body = x402.gate(conn, fac, {"payment_id": "p1", "signature": "ok"}, req, params, "v1", compute)
    assert st == 200 and body["payment"]["settled"] and len(calls) == 1
    st, body = x402.gate(conn, fac, {"payment_id": "p1", "signature": "ok"}, req, params, "v1", compute)
    assert st == 200 and body["payment"].get("replay") and len(calls) == 1  # no recompute, no resettle
    assert len(fac.settled) == 1
    # crash boundary: settle succeeds at the facilitator but we never see the receipt
    st, body = x402.gate(conn, fac, {"payment_id": "p2", "signature": "crash"}, req, params, "v1", compute)
    assert st == 402 and "uncertain" in body["error"] and len(calls) == 2
    row = conn.execute("SELECT state FROM x402_replays WHERE payment_id='p2'").fetchone()
    assert row["state"] == "computed"
    st, body = x402.gate(conn, fac, {"payment_id": "p2", "signature": "crash"}, req, params, "v1", compute)
    assert st == 200 and body["payment"].get("reconciled") and len(calls) == 2  # reconciled, not resettled
    assert len(fac.settled) == 2
    st, body = x402.gate(conn, fac, {"payment_id": "p3", "signature": "bad"}, req, params, "v1", compute)
    assert st == 402 and len(calls) == 2
    assert conn.execute("SELECT COUNT(*) FROM payments WHERE provider='x402' AND state='settled'").fetchone()[0] == 2  # p1 + reconciled p2
    # one payment_id is bound to one request: p1 cannot be stretched over a different token or collector version
    st, body = x402.gate(conn, fac, {"payment_id": "p1", "signature": "ok"}, req, {"token": OTHER, "chain_id": 4663}, "v1", compute)
    assert st == 402 and "bound" in body["error"] and len(calls) == 2
    st, body = x402.gate(conn, fac, {"payment_id": "p1", "signature": "ok"}, req, params, "v2", compute)
    assert st == 402 and "bound" in body["error"] and len(calls) == 2
    # a failing compute releases the claim and charges nothing; the same payment can retry
    def boom():
        raise RuntimeError("rpc down")
    st, body = x402.gate(conn, fac, {"payment_id": "p4", "signature": "ok"}, req, params, "v1", boom)
    assert st == 503 and "p4" not in fac.settled and conn.execute("SELECT COUNT(*) FROM x402_replays WHERE payment_id='p4'").fetchone()[0] == 0
    st, body = x402.gate(conn, fac, {"payment_id": "p4", "signature": "ok"}, req, params, "v1", compute)
    assert st == 200 and len(calls) == 3
    # a concurrent duplicate sees the 'computing' claim and is told to retry instead of recomputing
    with db.tx(conn):
        rh = x402.request_hash({"params": params, "requirements": req})
        conn.execute("INSERT INTO x402_replays (key, payment_id, request_hash, collector_version, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                     (f"p5:{rh}:v1", "p5", rh, "v1", "computing", db.now(), db.now()))
    st, body = x402.gate(conn, fac, {"payment_id": "p5", "signature": "ok"}, req, params, "v1", compute)
    assert st == 402 and "being processed" in body["error"] and len(calls) == 3
    # the same claim left by a dead process (older than CLAIM_S) is taken over exactly once and completed
    conn.execute("UPDATE x402_replays SET updated_at=? WHERE payment_id='p5'", (db.now() - x402.CLAIM_S - 1,))
    st, body = x402.gate(conn, fac, {"payment_id": "p5", "signature": "ok"}, req, params, "v1", compute)
    assert st == 200 and len(calls) == 4 and "p5" in fac.settled
    # the schema itself refuses a second row for one payment_id (the race two concurrent gates would otherwise win)
    with pytest.raises(db.DbError, match="UNIQUE"), db.tx(conn):
        conn.execute("INSERT INTO x402_replays (key, payment_id, request_hash, collector_version, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                     ("p5:other:v1", "p5", "other", "v1", "computing", db.now(), db.now()))


# ---------------------------------------------------------------- monitor
class ChainFake:
    """Canonical chain as {number: hash}; logs listed with their block hash so a reorg can swap both."""
    chain_id = 4663
    cache = None

    def __init__(self, latest, finalized):
        self.latest, self.finalized = latest, finalized
        self.hashes = {}
        self.logs = []
        self.fail = False

    def h(self, n):
        return self.hashes.get(n, "0x" + f"{n:064x}")

    def rpc(self, calls, batch=40, soft=False, errors=None):
        out = []
        for m, p in calls:
            if self.fail:
                errors.append("boom")
                out.append(None)
                continue
            if m == "eth_getBlockByNumber":
                tag = p[0]
                n = self.latest if tag == "latest" else self.finalized if tag == "finalized" else int(tag, 16)
                out.append({"number": hex(n), "hash": self.h(n)})
            elif m == "eth_getLogs":
                f = p[0]
                lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
                out.append([lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi and lg["topics"][1] == f["topics"][1]])
            else:
                out.append(None)
        return out


def _lg(block, frm, h, idx=0):
    return {"transactionHash": "0x" + f"{block:x}{idx:x}".rjust(64, "0"), "blockNumber": hex(block), "logIndex": hex(idx), "address": TOKEN,
            "topics": [TRANSFER, pad(frm), pad(OTHER)], "data": ONE, "blockHash": h}


def _sub(conn, cid, start_block):
    t = db.now()
    sid = db.new_id()
    with db.tx(conn):
        conn.execute("INSERT INTO monitoring_subscriptions (id, case_id, chain_id, token, addresses_json, state, starts_at, ends_at, start_block, channel, destination, next_due_at, updated_at)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (sid, cid, 4663, TOKEN, db.dumps([VICTIM]), "active", t, t + 86400, start_block, "file", None, t, t))
    return conn.execute("SELECT * FROM monitoring_subscriptions WHERE id=?", (sid,)).fetchone()


def test_monitor_reorg_rolls_back_provisional_and_alerts_only_finalized(conn, tmp_path):
    cid, _ = _case(conn)
    chain = ChainFake(latest=1_000, finalized=900)
    chain.logs = [_lg(950, VICTIM, chain.h(950)), _lg(880, VICTIM, chain.h(880))]
    sub = _sub(conn, cid, start_block=850)
    out = monitor.tick(chain, conn, sub)
    assert out["error"] is None and out["new_final"] == 1 and out["new_provisional"] == 1 and out["notified"] == 1
    row = conn.execute("SELECT * FROM monitoring_subscriptions WHERE id=?", (sub["id"],)).fetchone()
    assert row["finalized_cursor"] == 900 and row["provisional_cursor"] == 1_000 and row["provisional_hash"] == chain.h(1_000)
    # reorg: blocks 940..1000 replaced; the block-950 transfer disappears, a different one appears at 951
    for n in range(940, 1_001):
        chain.hashes[n] = "0x" + f"{n:060x}dead"
    chain.logs = [_lg(951, VICTIM, chain.h(951), idx=3), _lg(880, VICTIM, chain.h(880))]
    chain.latest = 1_010
    out = monitor.tick(chain, conn, sub)
    assert out["reorg"] is True and out["error"] is None
    assert conn.execute("SELECT COUNT(*) FROM evidence_items WHERE case_id=?", (cid,)).fetchone()[0] == 2
    prov = conn.execute("SELECT block, finality FROM evidence_items WHERE case_id=? AND finality='provisional'", (cid,)).fetchall()
    assert [(r[0], r[1]) for r in prov] == [(951, "provisional")]  # orphaned 950 row gone, canonical 951 recorded
    assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1  # still only the finalized alert
    assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='reorg_rollback'").fetchone()[0] == 1
    # finality advances: 951 becomes final -> exactly one new alert keyed on the canonical hash; the 880 one is not re-sent
    chain.finalized = 960
    out = monitor.tick(chain, conn, sub)
    assert out["notified"] == 1
    assert conn.execute("SELECT finality FROM evidence_items WHERE block=951").fetchone()[0] == "finalized"
    sent = notify.deliver_pending(conn, outfile=tmp_path / "n.jsonl")
    assert sent == 2 and len((tmp_path / "n.jsonl").read_text().splitlines()) == 2
    assert monitor.tick(chain, conn, sub)["notified"] == 0  # idempotent


def test_monitor_degrades_on_repeated_failure_and_keeps_cursors(conn):
    cid, _ = _case(conn)
    chain = ChainFake(latest=1_000, finalized=900)
    sub = _sub(conn, cid, start_block=850)
    monitor.tick(chain, conn, sub)
    chain.fail = True
    for _ in range(monitor.DEGRADE_AFTER):
        assert monitor.tick(chain, conn, sub)["error"]
    row = conn.execute("SELECT * FROM monitoring_subscriptions WHERE id=?", (sub["id"],)).fetchone()
    assert row["state"] == "degraded" and row["finalized_cursor"] == 900 and row["last_error"]


# ---------------------------------------------------------------- assertions + reports
def test_record_result_three_valued_and_package_hash(conn):
    cid, _ = _case(conn)
    run = queue.lease(conn, "w")
    res = {"token": TOKEN, "complete": False, "triage": {"dev_sold_in_window": False, "coverage": {"window_s": 1800, "complete": False}},
           "coordinated_launch": {"verdict": None, "reason": "coverage incomplete"}, "launch_calldata": {"decoded": {"snipe_tax_exemptions": ["0x" + "1" * 40]}}}
    queue.finish(conn, run["id"], "w", "partial", result=res)
    cases.record_result(conn, cid, run["id"], "rug_screen", res)
    v = {r["predicate"]: r["verdict"] for r in conn.execute("SELECT predicate, verdict FROM assertions WHERE superseded_at IS NULL")}
    assert v["dev_sold_in_window"] == "unknown (incomplete)"  # incomplete coverage never yields 'no'
    res["triage"]["coverage"]["complete"] = True
    cases.record_result(conn, cid, run["id"], "rug_screen", res)
    assert conn.execute("SELECT verdict FROM assertions WHERE predicate='dev_sold_in_window' AND superseded_at IS NULL").fetchone()[0] == "no"
    v = {r["predicate"]: r["verdict"] for r in conn.execute("SELECT predicate, verdict FROM assertions WHERE superseded_at IS NULL")}
    assert v[cases.SIGNAL_PREDICATE] == "unknown (incomplete)" and v["snipe_tax_exemptions_declared"] == "YES"
    case = conn.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
    runs = conn.execute("SELECT * FROM investigation_runs WHERE case_id=?", (cid,)).fetchall()
    asr = conn.execute("SELECT * FROM assertions WHERE case_id=?", (cid,)).fetchall()
    pkg = reports.build(case, runs, [], asr, [], rpc_identity="https://rpc.example")
    assert reports.verify(pkg) and pkg["manifest"]["chain_id"] == 4663 and pkg["manifest"]["rpc"] == "https://rpc.example"
    tampered = json.loads(reports.to_json(pkg))
    tampered["assertions"][0]["verdict"] = "no"
    assert not reports.verify(tampered)
    html = reports.to_html(pkg)
    assert "unknown (incomplete)" in html and "<script" not in html


def test_case_token_and_abuse_brake(conn):
    cid, tok = _case(conn)
    assert cases.load(conn, cid, tok)["id"] == cid
    assert cases.load(conn, cid, tok + "x") is None and cases.load(conn, "nope", tok) is None
    for _ in range(cases.MAX_OPEN_PER_SUBJECT - 1):
        _case(conn)
    with pytest.raises(ValueError):
        _case(conn)
    with pytest.raises(ValueError):
        cases.create(conn, "rug", 4663, "not-an-address")


# ---------------------------------------------------------------- web + worker end to end
@pytest.fixture
def web(conn, monkeypatch):
    from fastapi.testclient import TestClient

    from service import app as A
    monkeypatch.setattr(A, "_conn", conn)
    monkeypatch.setenv("SLEUTH_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("SLEUTH_X402", "1")
    return TestClient(A.app)


def _fake_run_job(cfg, kind, payload, budget_s=None):
    if kind == "rug_screen":
        return "succeeded", {"token": payload["token"], "complete": True, "checked_through": 123, "gaps": [],
                             "triage": {"dev_sold_in_window": True, "window_s": 1800, "summary": "fake", "tracked": [{"addr": OTHER, "roles": ["launcher"], "sold": "1", "bought": "0", "balance_now": None, "balance_fresh": False}],
                                        "launch": {"launcher": OTHER}}, "coordinated_launch": {"verdict": False, "reason": "fake"}}
    if kind == "money_trail":
        return "succeeded", {"token": payload["token"], "complete": True, "checked_through": 125, "gaps": [],
                             "trail": {"wallets": [{"addr": w["addr"], "role": w["role"], "kind": "eoa", "label": "wallet", "disposals": [], "proceeds": {}, "hops": [], "endpoints": {}, "complete": True} for w in payload["wallets"]],
                                       "verdict": {"sale_proceeds_received": False, "token_parked_in_non_venue_contract": False, "token_sent_to_bridge": False,
                                                   "proceeds_left_wallet": None, "proceeds_reached_bridge_or_exchange": False}, "gaps": [], "complete": True}}
    if kind == "rug_deep":
        return "succeeded", {"token": payload["token"], "complete": True, "checked_through": 124, "gaps": [], "dev_history": {"dev": OTHER, "dev_ever_sold": True, "history_complete": True, "summary": "fake"},
                             "trace": {"verdict": {"direct_tx_connection": False, "shared_funding": None}}}
    return "partial", {"victim": payload["victim"], "complete": False, "gaps": [{"kind": "x", "detail": "y"}], "predicates": {"transfer_event_from_victim": True}, "causal": {"drain_cause": {"reason": "unknown"}}, "events": []}


def test_web_end_to_end(web, conn, monkeypatch):
    from service import worker as W
    monkeypatch.setattr(W, "run_job", _fake_run_job)
    r = web.post("/screen", data={"kind": "rug", "subject": TOKEN}, follow_redirects=False)
    assert r.status_code == 303
    path, q = r.headers["location"].split("?")
    k = q.split("=")[1]
    assert web.get(path + "?k=" + k).status_code == 200 and web.get(path + "?k=bad").status_code == 404
    w = W.Worker(conn=conn, cfg={})
    assert w.run_one() and w.run_one() and not w.run_one()  # screen, then the free money-trail follow-up it enqueued
    assert [r[0] for r in conn.execute("SELECT kind FROM investigation_runs ORDER BY created_at").fetchall()] == ["rug_screen", "money_trail"]
    page = web.get(path + "?k=" + k).text
    assert "dev_sold_in_window" in page and "YES" in page and "Create order" in page
    assert "Money trail" in page and "sale_proceeds_received" in page and "token_parked_in_non_venue_contract" in page
    assert web.post(path + "/checkout", data={"k": k}, follow_redirects=False).status_code == 303
    pid = conn.execute("SELECT id FROM payments").fetchone()[0]
    assert web.post(f"/admin/payments/{pid}/confirm", data={"tx_hash": "0x" + "ab" * 32}).status_code == 401
    from service import app as A
    assert web.post(f"/admin/payments/{pid}/confirm", data={"tx_hash": "0x" + "ab" * 32}, auth=("admin", "pw")).status_code == 403  # no CSRF token
    assert A._CSRF in web.get("/admin/payments", auth=("admin", "pw")).text
    r = web.post(f"/admin/payments/{pid}/confirm", data={"tx_hash": "0x" + "ab" * 32, "csrf": A._CSRF}, auth=("admin", "pw"), follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    assert w.run_one()  # deep run now leasable
    page = web.get(path + "?k=" + k).text
    assert "Entitled" in page and "dev_ever_sold_prior_launches" in page and "Monitoring" in page
    assert conn.execute("SELECT state FROM monitoring_subscriptions").fetchone()[0] == "active"
    pkg = web.get(path + "/export.json?k=" + k).json()
    assert reports.verify(pkg) and len(pkg["assertions"]) >= 3
    assert web.get(path + "/export.html?k=" + k).status_code == 200
    assert "no-referrer" in web.get("/").headers["referrer-policy"]


def test_x402_endpoint(web, monkeypatch):
    from service import app as A
    A.app.state.screen_runner = lambda p: {"token": p["token"], "complete": True}
    r = web.post("/v1/screen/token", json={"token": TOKEN})
    assert r.status_code == 402 and r.json()["accepts"][0]["scheme"] == "exact"
    hdr = json.dumps({"payment_id": "agent-1", "signature": "ok"})
    r = web.post("/v1/screen/token", json={"token": TOKEN}, headers={"PAYMENT-SIGNATURE": hdr})
    assert r.status_code == 200 and r.json()["payment"]["settled"]
    r = web.post("/v1/screen/token", json={"token": TOKEN}, headers={"PAYMENT-SIGNATURE": hdr})
    assert r.json()["payment"].get("replay")
    assert web.post("/v1/screen/token", json={"token": "zzz"}, headers={"PAYMENT-SIGNATURE": hdr}).status_code == 400


def test_drain_case_never_clean(web, conn, monkeypatch):
    from service import worker as W
    monkeypatch.setattr(W, "run_job", _fake_run_job)
    r = web.post("/screen", data={"kind": "drain", "subject": VICTIM, "incident_ts": "1761480000"}, follow_redirects=False)
    path, q = r.headers["location"].split("?")
    W.Worker(conn=conn, cfg={}).run_one()
    v = {r["predicate"]: r["verdict"] for r in conn.execute("SELECT predicate, verdict FROM assertions")}
    assert v["transfer_event_from_victim"] == "YES" and v["drain_cause"] == "unknown (incomplete)"
    assert "INCOMPLETE" in web.get(path + "?" + q).text
    assert web.post(path + "/checkout", data={"k": q.split("=")[1]}).status_code == 400  # no paid product for drain cases


def test_db_backup_and_wal(conn, tmp_path):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    _case(conn)
    db.backup(conn, tmp_path / "b.db")
    assert sqlite3.connect(tmp_path / "b.db").execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1
    assert os.path.exists(tmp_path / "b.db")


# ---------------------------------------------------------------- Codex review follow-ups (2026-10-26)
def test_retry_wait_at_cap_is_terminal_and_stale_finish_rejected(conn):
    cid, _ = _case(conn)
    rid = queue.lease(conn, "w")["id"]
    assert queue.finish(conn, rid, "w", "retry_wait", error="transient")
    assert conn.execute("SELECT state FROM investigation_runs WHERE id=?", (rid,)).fetchone()[0] == "queued"
    for _ in range(queue.MAX_ATTEMPTS - 1):
        assert queue.lease(conn, "w")["id"] == rid
        queue.finish(conn, rid, "w", "retry_wait", error="transient")
    row = conn.execute("SELECT state, error FROM investigation_runs WHERE id=?", (rid,)).fetchone()
    assert row["state"] == "failed_terminal" and "exhausted" in row["error"]  # never stranded as queued-but-unleasable
    rid2 = queue.enqueue(conn, cid, "y", {}, "v")
    queue.lease(conn, "w")
    conn.execute("UPDATE investigation_runs SET lease_expires_at=? WHERE id=?", (db.now() - 1, rid2))
    assert not queue.finish(conn, rid2, "w", "succeeded", result={})  # expired lease: a late result is dropped
    assert not queue.heartbeat(conn, rid2, "w")  # ...and a late heartbeat cannot revive it either
    assert conn.execute("SELECT lease_expires_at < ? FROM investigation_runs WHERE id=?", (db.now(), rid2)).fetchone()[0] == 1


def test_assertions_commit_with_run_state(conn, monkeypatch):
    from service import worker as W
    monkeypatch.setattr(W, "run_job", _fake_run_job)
    cid, _ = _case(conn)
    W.Worker(conn=conn, cfg={}).run_one()
    run = conn.execute("SELECT state FROM investigation_runs WHERE case_id=? AND kind='rug_screen'", (cid,)).fetchone()
    assert run["state"] == "succeeded" and conn.execute("SELECT COUNT(*) FROM assertions WHERE case_id=?", (cid,)).fetchone()[0] >= 2
    # a result the recorder cannot parse does not strand the run as 'leased'
    monkeypatch.setattr(W, "run_job", lambda *a, **k: ("succeeded", {"token": TOKEN, "triage": "not-a-dict"}))
    queue.enqueue(conn, cid, "rug_screen", {"token": TOKEN, "chain_id": 1}, "v2")
    W.Worker(conn=conn, cfg={}).run_one()
    assert conn.execute("SELECT COUNT(*) FROM investigation_runs WHERE state='leased'").fetchone()[0] == 0


def test_dev_ever_sold_no_requires_complete_history(conn):
    cid, _ = _case(conn)
    run = queue.lease(conn, "w")
    res = {"token": TOKEN, "dev_history": {"dev": OTHER, "dev_ever_sold": False, "history_complete": False}}
    queue.finish(conn, run["id"], "w", "partial", result=res)
    cases.record_result(conn, cid, run["id"], "rug_deep", res)
    assert conn.execute("SELECT verdict FROM assertions WHERE predicate='dev_ever_sold_prior_launches'").fetchone()[0] == "unknown (incomplete)"


def test_monitor_missing_hash_is_provider_failure_not_reorg(conn):
    cid, _ = _case(conn)
    chain = ChainFake(latest=1_000, finalized=900)
    sub = _sub(conn, cid, start_block=850)
    assert monitor.tick(chain, conn, sub)["error"] is None
    real = chain.rpc

    def flaky(calls, **kw):  # the cursor block's header comes back empty; everything else works
        out = real(calls, **kw)
        return [None if m == "eth_getBlockByNumber" and p[0] == hex(1_000) else o for (m, p), o in zip(calls, out)]
    chain.rpc = flaky
    out = monitor.tick(chain, conn, sub)
    assert out["reorg"] is False and out["error"] and "unavailable" in out["error"]
    assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='reorg_rollback'").fetchone()[0] == 0


def test_monitor_alerts_are_per_case(conn, tmp_path):
    a, _ = _case(conn)
    b, _ = cases.create(conn, "rug", 4663, OTHER)
    chain = ChainFake(latest=1_000, finalized=900)
    chain.logs = [_lg(880, VICTIM, chain.h(880))]
    for cid in (a, b):
        assert monitor.tick(chain, conn, _sub(conn, cid, start_block=850))["notified"] == 1  # same event, two customers, two alerts
    assert conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 2
    with db.tx(conn):  # a crashed delivery left a stale claim: it is reclaimed, not lost
        conn.execute("UPDATE notification_outbox SET state='sending', claimed_at=?", (db.now() - 3600,))
    assert notify.deliver_pending(conn, outfile=tmp_path / "n.jsonl") == 2


def test_expired_subscriptions_end_and_screen_brake(conn, web):
    cid, _ = _case(conn)
    sub = _sub(conn, cid, start_block=850)
    with db.tx(conn):
        conn.execute("UPDATE monitoring_subscriptions SET ends_at=? WHERE id=?", (db.now() - 1, sub["id"]))
    assert monitor.due(conn) == [] and conn.execute("SELECT state FROM monitoring_subscriptions WHERE id=?", (sub["id"],)).fetchone()[0] == "ended"
    from service import app as A
    A.SCREEN_BRAKE.hits.clear()
    for _ in range(A.SCREEN_BRAKE.limit):
        assert A.SCREEN_BRAKE.hit("1.2.3.4")
    assert not A.SCREEN_BRAKE.hit("1.2.3.4") and A.SCREEN_BRAKE.hit("5.6.7.8")
    A.SCREEN_BRAKE.hits.clear()


def test_monitor_cursor_only_advances_to_lowest_complete_address(conn):
    """Two watched addresses: the second one's scan fails, so the cursor must not move past what IT covered, or the
    next tick would resume beyond blocks that address was never scanned for."""
    cid, _ = _case(conn)
    chain = ChainFake(latest=1_000, finalized=900)
    chain.logs = [_lg(880, VICTIM, chain.h(880)), _lg(890, OTHER, chain.h(890))]
    sub = _sub(conn, cid, start_block=850)
    with db.tx(conn):
        conn.execute("UPDATE monitoring_subscriptions SET addresses_json=? WHERE id=?", (db.dumps([VICTIM, OTHER]), sub["id"]))
    real = chain.rpc

    def second_addr_fails(calls, **kw):
        out = real(calls, **kw)
        for i, (m, p) in enumerate(calls):
            if m == "eth_getLogs" and p[0]["topics"][1] == pad(OTHER):
                kw["errors"].append("rpc: no response")
                out[i] = None
        return out
    chain.rpc = second_addr_fails
    out = monitor.tick(chain, conn, sub)
    row = conn.execute("SELECT finalized_cursor, provisional_cursor FROM monitoring_subscriptions WHERE id=?", (sub["id"],)).fetchone()
    assert out["error"] and row["finalized_cursor"] is None and row["provisional_cursor"] is None  # nothing covered for OTHER
    assert conn.execute("SELECT COUNT(*) FROM evidence_items WHERE case_id=?", (cid,)).fetchone()[0] == 0
    chain.rpc = real
    out = monitor.tick(chain, conn, sub)
    assert out["error"] is None and out["new_final"] == 2  # both addresses' transfers recorded once the scan completes


def test_monitor_refuses_to_overwrite_a_cursor_another_worker_moved(conn):
    cid, _ = _case(conn)
    chain = ChainFake(latest=1_000, finalized=900)
    sub = _sub(conn, cid, start_block=850)
    real = chain.rpc

    def other_worker_commits_during_scan(calls, **kw):  # a competing tick lands between our RPC and our write
        with db.tx(conn):
            conn.execute("UPDATE monitoring_subscriptions SET finalized_cursor=950, finalized_hash=? WHERE id=?", (chain.h(950), sub["id"]))
        return real(calls, **kw)
    chain.rpc = other_worker_commits_during_scan
    out = monitor.tick(chain, conn, sub)
    assert "changed under us" in (out["error"] or "")
    row = conn.execute("SELECT finalized_cursor, last_error, state FROM monitoring_subscriptions WHERE id=?", (sub["id"],)).fetchone()
    assert row["finalized_cursor"] == 950 and row["last_error"] is None and row["state"] == "active"  # the other worker's write stands


def test_db_refuses_old_schema_version(tmp_path):
    p = tmp_path / "old.db"
    c = db.connect(p)
    c.execute("PRAGMA user_version=1")
    c.close()
    with pytest.raises(db.DbError, match="schema version 1"):
        db.connect(p)


def test_plain_language_layer_and_incident_time_form(web, conn, monkeypatch):
    from service import plain
    from service import worker as W
    assert plain.question("drain_cause") == "What caused the loss?" and plain.question("some_new_key") == "some new key"
    assert plain.meaning("drain_cause", "unknown (incomplete)").startswith("Events alone cannot say")
    assert plain.meaning("never_seen", "YES", "recorded reason") == "recorded reason"
    assert plain.gap_text("no_trace: internal transfers unknown")[0].startswith("Native-coin moves")
    assert plain.gap_text({"kind": "truncated", "detail": "x"}) == ("An explorer page was cut short, so part of a wallet's history was not read.", "truncated: x")
    assert plain.gap_text("something odd") == ("something odd", "something odd")
    # dropdown and date picker both become a unix time; the raw field (API/tests) still wins; garbage is a readable error
    assert plain.incident_time("1761480000", "24", "", "America/Los_Angeles") == (1761480000.0, None)
    assert plain.incident_time("", "24", "", "America/Los_Angeles", now=1_000_000) == (1_000_000 - 86400, 24)
    ts, h = plain.incident_time("", "exact", "2026-10-26T15:00", "America/Los_Angeles")
    assert h is None and ts == 1793052000.0  # 2026-10-26 15:00 PDT = 22:00 UTC
    assert plain.incident_time("", "", "", "America/Los_Angeles") == (None, None)
    # a stale picker value must not override the dropdown; bad inputs are readable errors, never silent defaults
    assert plain.incident_time("", "6", "2020-01-01T00:00", "America/Los_Angeles", now=1_000_000) == (1_000_000 - 6 * 3600, 6)
    for raw, when, local in [("", "exact", "yesterday-ish"), ("", "exact", ""), ("", "exact", "2026-10-26T15:00+00:00"),
                             ("", "exact", "2026-03-08T02:30"), ("nan", "", ""), ("inf", "", ""), ("-5", "", "")]:
        with pytest.raises(ValueError):
            plain.incident_time(raw, when, local, "America/Los_Angeles")
    # no window read (head unavailable) => the summary must not claim anything was checked
    head_fail = {"victim": VICTIM, "events": [], "gaps": ["head unavailable: boom"], "coverage": None, "input": {"incident_ts": None}}
    s = plain.summary({"kind": "drain"}, head_fail, [{"predicate": "transfer_event_from_victim", "verdict": "unknown (incomplete)"}])
    assert s == ["Nothing could be checked: the chain could not be read for this run, so no window was scanned."]
    fell_back = {"victim": VICTIM, "events": [], "gaps": ["time->block lookup failed (RpcError); used a head-relative window"],
                 "window": {"start": 1, "end": 9}, "input": {"incident_ts": 123.0}}
    assert plain.summary({"kind": "drain"}, fell_back, [])[0].startswith("The time you gave could not be mapped to blocks")
    monkeypatch.setattr(W, "run_job", _fake_run_job)
    r = web.post("/screen", data={"kind": "drain", "subject": VICTIM, "when": "6"}, follow_redirects=False)
    assert r.status_code == 303
    payload = json.loads(conn.execute("SELECT result_json FROM investigation_runs WHERE kind='drain_screen' ORDER BY created_at DESC LIMIT 1").fetchone()[0])["input"]
    assert payload["hours_after"] == 6 and payload["incident_ts"] is not None
    assert web.post("/screen", data={"kind": "drain", "subject": VICTIM, "when": "exact", "incident_local": "nope"}).text.count("could not be read") == 1
    W.Worker(conn=conn, cfg={}).run_one()
    page = web.get(r.headers["location"]).text
    assert "In plain words" in page and "What caused the loss?" in page and "Did tokens leave this wallet" in page
    assert "technical details" in page and "YES" in page
    # hours_after=0 must clamp to 1, not vanish (a queued run would otherwise default to 24)
    from service import cases as C
    cid, _ = C.create(conn, "drain", 4663, VICTIM, hours_after=0)
    assert json.loads(conn.execute("SELECT result_json FROM investigation_runs WHERE case_id=? ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()[0])["input"]["hours_after"] == 1
