"""Investigation jobs. Each job takes a read-only Client, returns a JSON-able dict with a `complete` flag, `gaps`, and
`checked_through`, and never raises on provider failure (the worker records a partial/failed run instead). Jobs call
the existing sleuth library (triage, trace, devhistory) plus the new collectors; they never import service.payments."""
import time

from sleuth import devhistory, trace, triage
from sleuth.client import ApiError, BudgetExceeded, Client
from sleuth.collectors import COLLECTOR_VERSION
from sleuth.collectors import drain as drain_inv
from sleuth.collectors.launchcall import launch_calldata
from sleuth.collectors.money import money_trail as _money_trail
from sleuth.collectors.ring import coordinated_launch
from sleuth.collectors.wallet import drain_screen
from sleuth.onchain import RpcError, transfer_logs

JOB_VERSION = f"jobs/0.1.0+{COLLECTOR_VERSION}"
SCREEN_BUDGET_S = 20.0
DEEP_BUDGET_S = 240.0
MONEY_BUDGET_S = 150.0   # explorer pages cost ~2-3 s each; 5 wallets x 6 pages plus hops
MONEY_MAX_WALLETS = 5
INVESTIGATE_BUDGET_S = 200.0   # presence on every configured chain, pages + concurrent tx fetches on two chains, 4 suspects; queue lease is 300 s
TX_BUDGET_S = 20.0
PROVIDER_ERRORS = (BudgetExceeded, ApiError, RpcError, OSError, ValueError, TypeError, KeyError)


def make_client(cfg, chain_id, budget_s):
    return Client(cfg, chain_id, budget_s=budget_s, cache=True)


def _head(client):
    try:
        return client.head()
    except PROVIDER_ERRORS as e:
        return {"chain_id": None, "block": None, "block_time": None, "time": time.time(), "error": f"{type(e).__name__}: {str(e)[:120]}"}


def rug_screen(client, token, window_s=1800):
    """Free screen: launch facts + 30-minute flow triage + launch calldata + coordinated-launch heuristic.
    Findings are shown before any paywall; the deep pass only adds history/trace, never withholds these."""
    out = {"job": "rug_screen", "job_version": JOB_VERSION, "token": token.lower(), "chain_id": client.chain_id,
           "head": _head(client), "gaps": [], "complete": False, "checked_through": None}
    try:
        tri = triage.run(client, token, window_s=window_s)
    except PROVIDER_ERRORS as e:
        out["gaps"].append(f"triage failed: {type(e).__name__}: {str(e)[:160]}")
        return out
    out["triage"] = tri
    out["gaps"].extend(tri.get("gaps") or [])
    launch_rec = tri.get("launch") or {}
    ctx = launch_rec.get("creation_tx") or launch_rec.get("tx")
    if ctx:
        out["launch_calldata"] = launch_calldata(client, ctx)
        out["gaps"].extend(out["launch_calldata"].get("gaps") or [])
        dec = out["launch_calldata"].get("decoded") or {}
        meta = tri.get("token_meta") or {}
        supply = meta.get("total_supply")
        lb = launch_rec.get("creation_block") or launch_rec.get("block")
        recs, recs_ok = None, False
        if lb is not None:
            try:  # launch window only (a few blocks): separate from triage's recent 30-minute flow
                n_err = len(client.errors)
                recs = transfer_logs(client, token, int(lb), int(lb) + 3)
                recs_ok = len(client.errors) == n_err  # only failures of THIS scan make the verdict unknown
            except PROVIDER_ERRORS as e:
                out["gaps"].append(f"launch-window transfers unavailable: {type(e).__name__}: {str(e)[:120]}")
        if supply and lb is not None and recs is not None:
            out["coordinated_launch"] = coordinated_launch(recs, int(lb), int(supply), exemptions=dec.get("snipe_tax_exemptions", ()),
                                                           market=launch_rec.get("market"), coverage_complete=recs_ok)
        else:
            out["coordinated_launch"] = {"verdict": None, "reason": "launch block, supply or launch-window transfers unavailable"}
    else:
        out["coordinated_launch"] = {"verdict": None, "reason": "launch tx not identified; launchpad calldata not checked"}
    out["complete"] = not out["gaps"]
    out["checked_through"] = (out["head"] or {}).get("block")
    return out


def rug_deep(client, token, tracked=()):
    """Paid pass: dev history across prior launches + wallet trace for tracked addresses. Deterministic, no prose."""
    out = {"job": "rug_deep", "job_version": JOB_VERSION, "token": token.lower(), "chain_id": client.chain_id,
           "head": _head(client), "gaps": [], "complete": False, "checked_through": None}
    try:
        out["dev_history"] = devhistory.run(client, token)
        out["gaps"].extend(out["dev_history"].get("gaps") or [])
    except PROVIDER_ERRORS as e:
        out["gaps"].append(f"dev history failed: {type(e).__name__}: {str(e)[:160]}")
    addrs = [a.lower() for a in tracked if a][:8]
    if addrs:
        try:
            dev = (out.get("dev_history") or {}).get("dev")
            out["trace"] = trace.group(client, addrs, token, {dev} if dev else set())
            out["gaps"].extend(out["trace"].get("gaps") or [])
        except PROVIDER_ERRORS as e:
            out["gaps"].append(f"trace failed: {type(e).__name__}: {str(e)[:160]}")
    out["complete"] = not out["gaps"]
    out["checked_through"] = (out["head"] or {}).get("block")
    return out


def drain(client, victim, incident_ts=None, hours_after=24):
    """Wallet-drain event screen (beta). Event-named predicates only; never a clean negative (see collectors.wallet)."""
    try:
        out = drain_screen(client, victim, incident_ts=incident_ts, hours_after=hours_after)
    except PROVIDER_ERRORS as e:
        out = {"stage": "drain_screen", "victim": victim.lower(), "chain_id": client.chain_id, "complete": False,
               "gaps": [{"kind": "rpc_error", "detail": f"{type(e).__name__}: {str(e)[:160]}"}], "events": [], "predicates": {}}
    out.update(job="drain_screen", job_version=JOB_VERSION)
    out.setdefault("checked_through", (out.get("coverage") or {}).get("checked_through"))
    return out


def drain_investigation(client, victim, incident_ts=None):
    """Free follow-up to the drain screen: how the assets left (mechanism per transaction), where they went (recipients
    and submitters profiled), what else the same submitter hit, and whether the wallet is STILL delegated. Multi-chain:
    every configured chain is checked for presence; the two busiest are read. See sleuth.collectors.drain."""
    out = {"job": "drain_investigation", "job_version": JOB_VERSION, "victim": victim.lower(), "chain_id": client.chain_id,
           "head": _head(client), "gaps": [], "complete": False, "checked_through": None}
    try:
        out["investigation"] = drain_inv.investigate(client, victim, incident_ts=incident_ts)
    except PROVIDER_ERRORS as e:
        out["gaps"].append(f"investigation failed: {type(e).__name__}: {str(e)[:160]}")
        return out
    out["gaps"] = [f"{g.get('kind')}: {g.get('detail')}" for g in out["investigation"].get("gaps") or []]
    out["complete"] = bool(out["investigation"].get("complete"))
    out["checked_through"] = (out["head"] or {}).get("block") if out["complete"] else None
    return out


def tx_explain(client, tx_hash):
    """Single-transaction explainer (entry point for "I have a hash"): roles, EIP-7702 authorizations, transfers and the
    mechanism for every account whose assets left in it."""
    out = {"job": "tx_explain", "job_version": JOB_VERSION, "tx": tx_hash.lower(), "chain_id": client.chain_id,
           "head": _head(client), "gaps": [], "complete": False, "checked_through": None}
    try:
        out["explain"] = drain_inv.explain_tx(client, tx_hash)
    except PROVIDER_ERRORS as e:
        out["gaps"].append(f"explain failed: {type(e).__name__}: {str(e)[:160]}")
        return out
    out["gaps"] = [f"{g.get('kind')}: {g.get('detail')}" for g in out["explain"].get("gaps") or []]
    out["complete"] = bool(out["explain"].get("complete"))
    out["checked_through"] = (out["head"] or {}).get("block") if out["complete"] else None
    return out


def money_trail(client, token, wallets=()):
    """Free follow-up to the screen: follow sale proceeds from the launcher and top sellers hop by hop (see
    sleuth.collectors.money). High-volume wallets (bots, hot wallets) are listed but not followed."""
    out = {"job": "money_trail", "job_version": JOB_VERSION, "token": token.lower(), "chain_id": client.chain_id,
           "head": _head(client), "gaps": [], "complete": False, "checked_through": None}
    ws = [(w["addr"], w.get("role") or "tracked") for w in wallets if isinstance(w, dict) and w.get("addr")][:MONEY_MAX_WALLETS]
    if not ws:
        out["gaps"].append("no wallets to follow (screen found no launcher or sellers)")
        return out
    try:
        out["trail"] = _money_trail(client, token, ws)
    except PROVIDER_ERRORS as e:
        out["gaps"].append(f"money trail failed: {type(e).__name__}: {str(e)[:160]}")
        return out
    out["gaps"] = [f"{g.get('kind')}: {g.get('detail')}" for g in out["trail"].get("gaps") or []]
    out["complete"] = bool(out["trail"].get("complete"))
    out["checked_through"] = (out["head"] or {}).get("block") if out["complete"] else None
    return out


JOBS = {"rug_screen": (rug_screen, SCREEN_BUDGET_S), "rug_deep": (rug_deep, DEEP_BUDGET_S), "drain_screen": (drain, SCREEN_BUDGET_S),
        "money_trail": (money_trail, MONEY_BUDGET_S), "drain_investigation": (drain_investigation, INVESTIGATE_BUDGET_S),
        "tx_explain": (tx_explain, TX_BUDGET_S)}


def run_job(cfg, kind, payload, budget_s=None):
    """Dispatch by kind. Returns (state, result): state is succeeded / partial / failed_terminal."""
    if kind not in JOBS:
        return "failed_terminal", {"error": f"unknown job kind {kind}"}
    fn, default_budget = JOBS[kind]
    try:
        client = make_client(cfg, int(payload["chain_id"]), budget_s or default_budget)
        if kind == "rug_screen":
            res = fn(client, payload["token"], window_s=int(payload.get("window_s", 1800)))
        elif kind == "rug_deep":
            res = fn(client, payload["token"], tracked=payload.get("tracked") or ())
        elif kind == "money_trail":
            res = fn(client, payload["token"], wallets=payload.get("wallets") or ())
        elif kind == "drain_investigation":
            res = fn(client, payload["victim"], incident_ts=payload.get("incident_ts"))
        elif kind == "tx_explain":
            res = fn(client, payload["tx"])
        else:
            res = fn(client, payload["victim"], incident_ts=payload.get("incident_ts"), hours_after=int(payload.get("hours_after") or 24))
        res["requests"] = getattr(client, "requests", None)
        return ("succeeded" if res.get("complete") else "partial"), res
    except (KeyError, ValueError, TypeError, SystemExit) as e:
        return "failed_terminal", {"error": f"bad input: {type(e).__name__}: {str(e)[:160]}"}
