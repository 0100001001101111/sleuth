"""Case lifecycle: create (free screen), capability-token access, entitlement after settlement (deep run + 30-day
monitoring), and turning job results into three-valued assertions. No external side effects."""
import hashlib
import hmac
import re
import secrets

from sleuth.collectors.money import RULE_VERSION as MONEY_RULE
from sleuth.collectors.ring import RULE_VERSION as RING_RULE

from . import db, queue
from .jobs import JOB_VERSION, MONEY_MAX_WALLETS

ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
MONITOR_DAYS = 30
MAX_OPEN_PER_SUBJECT = 3  # abuse brake: free screens of the same address are reused, not re-run
MAX_CASES_PER_HOUR = 60   # service-wide brake: one host, one worker; beyond this the queue only grows
MAX_QUEUED_RUNS = 50      # back-pressure: refuse new free screens while this many runs wait
SIGNAL_PREDICATE = "early_buy_concentration_over_threshold"  # a threshold signal, NOT a finding of coordination


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def valid_addr(a):
    return bool(a and ADDR.match(a.strip()))


def create(conn, kind, chain_id, subject, email=None, incident_ts=None, claimed_loss=None):
    """Create a case and its free screen run. Returns (case_id, access_token). The token is shown once; only its hash is stored."""
    if kind not in ("rug", "drain"):
        raise ValueError("kind must be rug or drain")
    if not valid_addr(subject):
        raise ValueError("subject must be a 0x address")
    subject = subject.strip().lower()
    token = secrets.token_urlsafe(24)
    cid = db.new_id()
    with db.tx(conn):
        n = conn.execute("SELECT COUNT(*) FROM cases WHERE subject=? AND kind=? AND status='open' AND created_at>?", (subject, kind, db.now() - 3600)).fetchone()[0]
        if n >= MAX_OPEN_PER_SUBJECT:
            raise ValueError("too many open screens for this address in the last hour; open an existing one")
        total = conn.execute("SELECT COUNT(*) FROM cases WHERE created_at>?", (db.now() - 3600,)).fetchone()[0]
        queued = conn.execute("SELECT COUNT(*) FROM investigation_runs WHERE state IN ('queued','leased')").fetchone()[0]
        if total >= MAX_CASES_PER_HOUR or queued >= MAX_QUEUED_RUNS:
            raise ValueError("the service is at capacity right now; try again in a few minutes")
        conn.execute("INSERT INTO cases (id, created_at, kind, chain_id, subject, email, status, product, access_hash, incident_ts, claimed_loss)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?)", (cid, db.now(), kind, int(chain_id), subject, (email or None), "open", "free_screen", _hash(token), incident_ts, claimed_loss))
        db.audit(conn, "web", "case_created", cid, {"kind": kind, "subject": subject})
    payload = {"chain_id": int(chain_id), "token": subject} if kind == "rug" else {"chain_id": int(chain_id), "victim": subject, "incident_ts": incident_ts}
    queue.enqueue(conn, cid, "rug_screen" if kind == "rug" else "drain_screen", payload, JOB_VERSION)
    return cid, token


def load(conn, case_id, token):
    """Capability check: constant-time compare of the token hash. Returns the case row or None."""
    if not case_id or not token:
        return None
    row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not row or not hmac.compare_digest(row["access_hash"], _hash(token)):
        return None
    return row


def entitle(conn, case_id, actor="system"):
    """After settlement: flip product, enqueue the paid deep pass, start a 30-day monitoring subscription (rug cases)."""
    case = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        raise ValueError("case not found")
    with db.tx(conn):
        conn.execute("UPDATE cases SET product='founding_case' WHERE id=?", (case_id,))
        db.audit(conn, actor, "case_entitled", case_id)
    if case["kind"] == "rug":
        tracked = []
        last = conn.execute("SELECT result_json FROM investigation_runs WHERE case_id=? AND kind='rug_screen' AND state IN ('succeeded','partial') ORDER BY finished_at DESC LIMIT 1",
                            (case_id,)).fetchone()
        res = (db.loads(last["result_json"]) if last else None) or {}
        tri = res.get("triage") or {}
        tracked = [t.get("addr") for t in (tri.get("tracked") or []) if isinstance(t, dict) and t.get("addr")]
        launcher = ((tri.get("launch") or {}).get("launcher"))
        addrs = sorted({a.lower() for a in tracked + ([launcher] if launcher else [])})[:12]
        queue.enqueue(conn, case_id, "rug_deep", {"chain_id": case["chain_id"], "token": case["subject"], "tracked": addrs}, JOB_VERSION, requires_entitlement=True)
        with db.tx(conn):
            exists = conn.execute("SELECT id FROM monitoring_subscriptions WHERE case_id=? AND state IN ('active','degraded')", (case_id,)).fetchone()
            if not exists:
                t = db.now()
                conn.execute("INSERT INTO monitoring_subscriptions (id, case_id, chain_id, token, addresses_json, state, starts_at, ends_at, channel, destination, next_due_at, updated_at)"
                             " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (db.new_id(), case_id, case["chain_id"], case["subject"], db.dumps(addrs or [case["subject"]]), "active", t,
                                                                   t + MONITOR_DAYS * 86400, "file", case["email"], t, t))
    return True


def _put(conn, case_id, run_id, subject, predicate, obj, verdict, reason, rule):
    word = {True: "YES", False: "no"}.get(verdict, "unknown (incomplete)")
    conn.execute("UPDATE assertions SET superseded_at=? WHERE case_id=? AND subject=? AND predicate=? AND superseded_at IS NULL", (db.now(), case_id, subject, predicate))
    conn.execute("INSERT INTO assertions (id, case_id, run_id, subject, predicate, object, verdict, reason, rule_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (db.new_id(), case_id, run_id, subject, predicate, obj, word, (reason or "")[:300], rule, db.now()))


def follow_up_tx(conn, case_id, kind, result):
    """Enqueue the free money trail once a rug screen has named wallets to follow. Runs INSIDE the parent run's
    finish transaction (queue.finish's `after`), so the child exists iff the parent's terminal state committed.
    Idempotent (the queue hashes the input). Priority within the cap: launcher, top sellers, then other tracked
    wallets with their triage role; omitted candidates are listed in the payload for the case page."""
    if kind != "rug_screen":
        return None
    tri = result.get("triage") or {}
    launcher = (tri.get("launch") or {}).get("launcher")
    cands = ([(launcher, "launcher")] if launcher else []) + \
        [(t.get("addr"), "top_seller") for t in tri.get("top_sellers") or [] if isinstance(t, dict)] + \
        [(t.get("addr"), t.get("role") or "tracked") for t in tri.get("tracked") or [] if isinstance(t, dict)]
    seen, wallets = set(), []
    for addr, role in cands:
        a = (addr or "").lower()
        if a and a not in seen:
            seen.add(a)
            wallets.append({"addr": a, "role": str(role)})
    if not wallets:
        return None
    case = conn.execute("SELECT chain_id, subject FROM cases WHERE id=?", (case_id,)).fetchone()
    if not case:
        return None
    payload = {"chain_id": case["chain_id"], "token": case["subject"], "wallets": wallets[:MONEY_MAX_WALLETS],
               "omitted": [w["addr"] for w in wallets[MONEY_MAX_WALLETS:]]}
    return queue.enqueue_tx(conn, case_id, "money_trail", payload, JOB_VERSION)


def record_result(conn, case_id, run_id, kind, result):
    """Write separate, three-valued assertions from a finished job (own transaction). See record_result_tx."""
    with db.tx(conn):
        record_result_tx(conn, case_id, run_id, kind, result)


def record_result_tx(conn, case_id, run_id, kind, result):
    """Same as record_result but for callers already inside db.tx() (the worker commits these with the run state).
    Incomplete coverage => unknown, never a negative."""
    if kind == "rug_screen":
        tri = result.get("triage") or {}
        tok = result.get("token")
        cov = tri.get("coverage") or {}
        cov_ok = bool(cov.get("complete"))  # the transfer-log stage's own coverage (triage.coverage), not the explorer side checks
        ds = tri.get("dev_sold_in_window")  # triage already answers None when its own preconditions fail; a 'no' additionally needs complete logs
        _put(conn, case_id, run_id, tok, "dev_sold_in_window", str(cov.get("window_s", 1800)), ds if ds is True or (ds is False and cov_ok) else None,
             "from triage transfer-log stage" + ("" if cov_ok else "; transfer-log coverage incomplete"), "triage/dev_sold")
        cl = result.get("coordinated_launch") or {}
        thr = cl.get("thresholds") or {}
        _put(conn, case_id, run_id, tok, SIGNAL_PREDICATE, db.dumps(thr) if thr else None, cl.get("verdict"),
             "threshold rule over early buyers; says nothing about who controls them. " + (cl.get("reason") or ""), RING_RULE)
        lc = (result.get("launch_calldata") or {}).get("decoded") or {}
        if lc:
            _put(conn, case_id, run_id, tok, "snipe_tax_exemptions_declared", str(len(lc.get("snipe_tax_exemptions") or [])),
                 bool(lc.get("snipe_tax_exemptions")), "decoded from launch calldata", "launchcall/0.1")
    elif kind == "money_trail":
        trail = result.get("trail") if isinstance(result.get("trail"), dict) else {}
        tv = trail.get("verdict") if isinstance(trail.get("verdict"), dict) else {}
        n = len(trail.get("wallets") or [])
        complete = trail.get("complete") is True and n > 0
        for k in ("sale_proceeds_received", "token_parked_in_non_venue_contract", "token_sent_to_bridge", "proceeds_left_wallet", "proceeds_reached_bridge_or_exchange"):
            if k in tv:
                val = tv[k]
                _put(conn, case_id, run_id, result.get("token"), k, f"{n} wallets",
                     val if val is True or (val is False and complete) else None,  # a 'no' needs a complete trail; a 'YES' stands on its own
                     "explorer pages for launcher + top sellers; a disposal with no quote asset credited is a transfer, not a sale; "
                     "ownership of endpoints unknown" + ("" if complete else "; trail incomplete"), MONEY_RULE)
    elif kind == "rug_deep":
        dh = result.get("dev_history") or {}
        if dh:
            v, complete = dh.get("dev_ever_sold"), bool(dh.get("history_complete"))
            _put(conn, case_id, run_id, result.get("token"), "dev_ever_sold_prior_launches", dh.get("dev"),
                 v if v is True or (v is False and complete) else None,  # a 'no' needs complete history; a 'YES' stands on its own
                 "across launches attributed to the same dev" + ("" if complete else "; history incomplete"), "devhistory")
        tv = (result.get("trace") or {}).get("verdict") or {}
        for k in ("direct_tx_connection", "shared_funding", "coordinated_trading", "batch_funded", "dev_connection"):
            if k in tv:
                val = tv[k]
                _put(conn, case_id, run_id, result.get("token"), k, None, val if isinstance(val, bool) else None,
                     "services excluded; ownership unknown", "trace/group")
    elif kind == "drain_screen":
        for name, val in (result.get("predicates") or {}).items():
            _put(conn, case_id, run_id, result.get("victim"), name, None, val, "event-named predicate over the incident window", "wallet/drain_screen")
        for name, info in (result.get("causal") or {}).items():
            _put(conn, case_id, run_id, result.get("victim"), name, None, None, (info or {}).get("reason") if isinstance(info, dict) else str(info), "wallet/causal")
    db.audit(conn, "worker", "assertions_recorded", case_id, {"run": run_id, "kind": kind})
