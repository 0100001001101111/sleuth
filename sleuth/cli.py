"""sleuth CLI.

  sleuth triage TOKEN [--window 30m] [--dev ADDR,..] [--track ADDR,..] [--budget 20]
  sleuth trace ADDR[,ADDR..] --token TOKEN [--dev ADDR,..] [--budget 70]
  sleuth burn TX_HASH [--token TOKEN]      |  sleuth burn TOKEN --window 24h
  sleuth contract ADDR
  sleuth watch TOKEN [add ADDR ROLE "reason" [--tx HASH]]
Every command writes full JSON evidence to ~/sleuth/data/runs/ and prints a short summary.
"""
import argparse, json, re, sys, time
from pathlib import Path
from . import config as C, triage as T, trace as R, burn as B, contract as K, report
from .client import Client, BudgetExceeded
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


def _save(kind, out):
    p = C.DATA / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}_{kind}.json"
    p.write_text(json.dumps(out, indent=1, default=str))
    return p


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
    cl = Client(cfg, ns.chain, budget_s=budget)
    try:
        if ns.cmd == "triage":
            if not ADDR.match(ns.token):
                raise SystemExit("token must be an address")
            try:
                out = T.run(cl, ns.token, _window(ns.window), _addrs(ns.track), _addrs(ns.dev))
            except BudgetExceeded:
                raise SystemExit(f"budget {budget}s exhausted before first result; retry with --budget or smaller --window")
            text = report.triage(out, cl)
        elif ns.cmd == "trace":
            wl = Watchlist(cl.chain_id, ns.token)
            dev = set(_addrs(ns.dev)) | set(wl.by_role("launcher", "dev", "initial_recipient"))
            lo = (wl.data.get("launch") or {}).get("creation_block")
            out = R.group(cl, _addrs(ns.addrs), ns.token, dev, wl.data.get("pools") or [], lo)
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
                text = f"**{len(out['burns'])} dead-address transfers** in last {ns.window} (blocks {out['from_block']}-{out['to_block']}). totalSupply now {out['total_supply_now']}.\n" + "\n".join(
                    f"- {b['amount']/1e18:,.0f} from {report.short(b['from'])} at {report.local(b['ts'], tz)} [tx]({cl.link('tx', b['tx'])})" for b in out["burns"][:8])
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
