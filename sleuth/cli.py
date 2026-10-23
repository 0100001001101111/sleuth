"""sleuth CLI.

  sleuth triage TOKEN [--window 30m] [--dev ADDR,..] [--track ADDR,..] [--budget 20]
  sleuth dev-history TOKEN [--dev ADDR,..] [--budget 70]   # has the dev EVER sold (full life)
  sleuth trace ADDR[,ADDR..] --token TOKEN [--dev ADDR,..] [--budget 70]
  sleuth burn TX_HASH [--token TOKEN]      |  sleuth burn TOKEN --window 24h
  sleuth tx TX_HASH                        # decode one tx: all token flows + swap evidence
  sleuth contract ADDR
  sleuth enrich RUN_JSON                   # retry failed ETH-funding checks of a saved triage run
  sleuth watch TOKEN [add ADDR ROLE "reason" [--tx HASH]]
Every command writes full JSON evidence to ~/sleuth/data/runs/ and prints a short summary.
"""
import argparse, json, re, sys, time
from pathlib import Path
from . import config as C, triage as T, trace as R, burn as B, contract as K, devhistory as D, txdecode as X, report
from .client import Client, BudgetExceeded, ApiError
from .onchain import RpcError
from .watchlist import Watchlist

ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
TXH = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _window(s):
    m = re.fullmatch(r"(\d+)([smhd])", s or "30m")
    if not m:
        raise SystemExit("window like 30m, 2h, 1d")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _addrs(s):
    out = [a.strip() for a in (s or "").split(",") if a.strip()]
    for a in out:
        if not ADDR.match(a):
            raise SystemExit(f"bad address: {a}")
    return out


def _typo_check(addrs, known):
    """Warn when a traced address is NOT in the watchlist but matches a watchlist entry on the first 4 and last
    4 hex digits — exactly the shape a short display shows (0x2a71…82ec), so exactly what an address wrongly
    reconstructed from one matches. A typo'd address traces clean, so the question must be asked loudly
    (V4M, 2026-10-22). Chance match for an unrelated pair is 16^-8: warnings stay rare."""
    warns = []
    kl = {k.lower() for k in known}
    for a in addrs:
        al = a.lower()
        if al in kl:
            continue
        for k in kl:
            if k[2:6] == al[2:6] and k[-4:] == al[-4:]:
                warns.append(f"ADDRESS TYPO? {a} is not in the watchlist but matches watchlist entry {k} on its "
                             "first 4 and last 4 hex digits — reconstructed from a short display? Verify before "
                             "trusting a clean trace")
    return warns


def _save(kind, out):
    # ms suffix: two runs in the same second (watch loops, scripts) must not overwrite each other
    p = C.DATA / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}_{kind}.json"
    p.write_text(json.dumps(out, indent=1, default=str))
    return p


def _enrich(cl, path):
    """Reload a saved triage run and retry the sellers whose ETH-funding check failed (explorer slow)."""
    p = Path(path)
    data = json.loads(p.read_text())
    if data.get("stage") != "triage":
        raise SystemExit("enrich takes a triage run JSON (data/runs/*_triage.json)")
    roles = lambda t: set(t.get("roles") or [t.get("role")])
    dev_set = {t["addr"] for t in data.get("tracked") or [] if roles(t) & {"launcher", "dev"}}
    retried, fixed = 0, 0
    for s in data.get("top_sellers") or []:
        if s.get("eth_funders_internal") is not None and not s.get("eth_funding_partial"):
            continue  # complete already; a partial (first-page-only) result is re-fetched across all pages
        retried += 1
        a = s["addr"]
        # Own-tx filter rebuilt from the SAVED tx lists: txs beyond the saved caps are not covered, so a
        # proceeds-receiving tx outside them could still read as funding -- marked in gaps below.
        own = set(s.get("acquisition_txs") or []) | set(s.get("sell_txs") or [])
        items, params, page_failed = [], None, False
        for _ in range(3):
            fd = cl.get(f"/addresses/{a}/internal-transactions", params=params, soft=True)
            if fd is None:
                page_failed = True
                break
            items += fd.get("items") or []
            params = fd.get("next_page_params")
            if not params:
                break
        # a failed or leftover page means coverage is partial: found funders are still worth reporting, but an
        # EMPTY partial result must stay None -- never fabricate "no funding" from half the pages
        partial = page_failed or bool(params)
        funders = []
        for x in items:
            if ((x.get("to") or {}).get("hash") or "").lower() != a or int(x.get("value") or 0) <= 0:
                continue
            if x.get("error") or not x.get("success", True):
                continue  # reverted internal call: no ETH actually moved
            if x.get("transaction_hash") in own:
                continue
            f = x.get("from") or {}
            funders.append({"from": (f.get("hash") or "").lower(), "label": f.get("name"), "eth": round(int(x["value"]) / 1e18, 4),
                            "ts": x.get("timestamp"), "tx": x.get("transaction_hash"), "block": x.get("block_number"),
                            "pre_acquisition": bool(s.get("first_acquired_block") and (x.get("block_number") or 0) < s["first_acquired_block"])})
        if partial and not funders:
            continue  # still unknown -- leave None
        s["eth_funders_internal"] = funders[:6]
        if partial:
            s["eth_funding_partial"] = True
        else:
            s.pop("eth_funding_partial", None)  # full history read: clear a stale first-page-only marker
        if any(f["from"] in dev_set for f in funders):
            s["dev_linked"] = True
            s["eth_from_dev"] = True
        fixed += 1
    if fixed:
        data.setdefault("gaps", []).append(
            f"enriched {time.strftime('%Y-%m-%d %H:%M')}: ETH funding retried for {fixed}/{retried} seller(s); "
            "own-tx filter rebuilt from saved tx lists (proceeds in txs beyond the saved caps could read as funding)")
    out_p = p.with_name(p.stem + "_enriched.json")
    out_p.write_text(json.dumps(data, indent=1, default=str))
    return data, out_p, retried, fixed


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sleuth", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chain", help="chain id (default from config)")
    ap.add_argument("--json", action="store_true", help="print full JSON instead of summary")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("triage"); a.add_argument("token"); a.add_argument("--window", default="30m")
    a.add_argument("--dev"); a.add_argument("--track"); a.add_argument("--budget", type=float)
    b = sub.add_parser("trace"); b.add_argument("addrs"); b.add_argument("--token", required=True); b.add_argument("--dev"); b.add_argument("--budget", type=float)
    c = sub.add_parser("burn"); c.add_argument("target"); c.add_argument("--token"); c.add_argument("--window", default="24h")
    d = sub.add_parser("contract"); d.add_argument("addr")
    e = sub.add_parser("dev-history"); e.add_argument("token"); e.add_argument("--dev"); e.add_argument("--budget", type=float)
    f = sub.add_parser("tx"); f.add_argument("hash")
    g = sub.add_parser("enrich"); g.add_argument("file")
    w = sub.add_parser("watch"); w.add_argument("token"); w.add_argument("rest", nargs="*"); w.add_argument("--tx")
    ns = ap.parse_args(argv)
    cfg = C.load()

    if ns.cmd == "watch":
        wl = Watchlist(str(ns.chain or cfg["default_chain"]), ns.token)
        if ns.rest and ns.rest[0] == "add":
            if len(ns.rest) < 4:
                raise SystemExit("watch TOKEN add ADDR ROLE REASON")
            wl.add(ns.rest[1], ns.rest[2], " ".join(ns.rest[3:]), [ns.tx] if ns.tx else [])
            wl.save()
        print(json.dumps(wl.data, indent=1))
        return 0

    budget = getattr(ns, "budget", None) or cfg["budgets"].get(ns.cmd if ns.cmd in ("triage", "trace") else "trace")
    if ns.cmd == "enrich":
        cl = Client(cfg, ns.chain, budget_s=budget)
        try:
            data, out_p, retried, fixed = _enrich(cl, ns.file)
            print(report.triage(data, cl) if "token_meta" in data else json.dumps(data, indent=1, default=str)[:2000])
            print(f"\n[enriched {fixed}/{retried} seller(s); evidence: {out_p}]")
            return 0
        finally:
            cl.close()
    cl = Client(cfg, ns.chain, budget_s=budget)
    try:
        if ns.cmd == "triage":
            if not ADDR.match(ns.token):
                raise SystemExit("token must be an address")
            try:
                out = T.run(cl, ns.token, _window(ns.window), _addrs(ns.track), _addrs(ns.dev))
            except BudgetExceeded:
                raise SystemExit(f"budget {budget}s exhausted before first result; retry with --budget or smaller --window")
            except (RpcError, ApiError) as e:
                raise SystemExit(f"RPC failure, no result (NOT 'no activity'): {e}")
            text = report.triage(out, cl)
        elif ns.cmd == "dev-history":
            if not ADDR.match(ns.token):
                raise SystemExit("token must be an address")
            try:
                out = D.run(cl, ns.token, _addrs(ns.dev))
            except (RpcError, ApiError) as e:
                raise SystemExit(f"RPC failure, no result (NOT 'no selling'): {e}")
            text = report.dev_history(out, cl)
        elif ns.cmd == "tx":
            if not TXH.match(ns.hash):
                raise SystemExit("tx must be a 0x…64-hex transaction hash")
            out = X.decode(cl, ns.hash)
            text = report.txdecode(out, cl)
        elif ns.cmd == "trace":
            wl = Watchlist(cl.chain_id, ns.token)
            # launcher + dev only: a creation-tx recipient can be a bundled buyer, so it is not dev evidence
            dev = set(_addrs(ns.dev)) | set(wl.by_role("launcher", "dev"))
            lo = (wl.data.get("launch") or {}).get("creation_block")
            addrs = _addrs(ns.addrs)
            warns = _typo_check(addrs, wl.data["addresses"].keys())
            out = R.group(cl, addrs, ns.token, dev, wl.data.get("pools") or [], lo)
            if warns:
                out["gaps"] = warns + (out.get("gaps") or [])
            for wlt in out["wallets"]:
                if wlt["batch_siblings"] or any(x["via"] == "pool (sell)" for x in wlt["token_disposals"]):
                    wl.add(wlt["addr"], "traced", f"{wlt['label']}; traced {time.strftime('%Y-%m-%d')}",
                           [x["tx"] for x in wlt["token_disposals"][:2]] + [b["tx"] for b in wlt["batch_siblings"][:2]])
                for b in wlt["batch_siblings"]:
                    wl.add(b["sender"], "funder", f"batch-funded {len(b['recipients'])+1} wallets", [b["tx"]])
                    for r in b["recipients"]:
                        wl.add(r["addr"], "sibling", f"batch-funded with {wlt['addr'][:10]}", [b["tx"]])
            wl.save()
            text = report.trace(out, cl)
        elif ns.cmd == "burn":
            if TXH.match(ns.target):
                out = B.check_tx(cl, ns.target, ns.token)
                text = report.burn(out, cl)
            elif ADDR.match(ns.target):
                out = B.check_token(cl, ns.target, _window(ns.window))
                tz = cl.chain.get("tz", "UTC")
                dec = 10 ** out.get("decimals", 18)
                sym = out.get("symbol") or ""
                text = f"**{len(out['burns'])} dead-address transfers** in last {ns.window} (blocks {out['from_block']}-{out['to_block']}). totalSupply now {out['total_supply_now']}.\n" + "\n".join(
                    f"- {b['amount']/dec:,.0f} {sym} from {report.short(b['from'])} at {report.local(b['ts'], tz)} [tx]({cl.link('tx', b['tx'])})" for b in out["burns"][:8])
            else:
                raise SystemExit("burn target must be a tx hash or token address")
        elif ns.cmd == "contract":
            out = K.audit(cl, ns.addr)
            text = report.contract(out, cl)
        out["requests"] = cl.requests
        out["errors"] = cl.errors[:10]
        p = _save(ns.cmd, out)
        if ns.json:
            print(json.dumps(out, indent=1, default=str))
        else:
            print(text)
            print(f"\n[evidence: {p}]")
        return 0
    finally:
        cl.close()


if __name__ == "__main__":
    sys.exit(main())
