"""Scan recent Pons V2 launches and rank them by launcher (dev) selling. RPC-only, so it works while the explorer is slow.

  python3 scripts/pons_scan.py --hours 48 [--limit 400] [--out data/runs/pons_scan.json]

For each launch: launcher's token outflows netted at receipt level (sell to market/pool vs transfer to another wallet vs burn),
launcher's current balance, tokens still in the market/pool. 'rug_score' = % of supply the launcher sold + transferred out.
"""
import argparse, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sleuth import config as C, flows, launch, onchain
from sleuth.client import Client
from sleuth.labels import DEAD


def scan_token(cl, L, head, known_pools):
    tok, dev = L["token"], L["launcher"]
    meta = onchain.erc20_meta(cl, tok)
    if not meta:
        return {**L, "error": "not erc20"}
    dec, supply = meta["decimals"], meta.get("total_supply") or 0
    pools = set(known_pools) | {L["market"]}
    outs = onchain.transfer_logs(cl, tok, L["creation_block"], head["block"], frm=dev)
    ins = onchain.transfer_logs(cl, tok, L["creation_block"], head["block"], to=dev)
    txs = list(dict.fromkeys(x["tx"] for x in sorted(outs + ins, key=lambda x: (x["block"], x["idx"]))))
    partial = len(txs) > 60  # metrics below are then lower bounds; triage the token for the full picture
    recs = onchain.tx_records(cl, txs[:60], tok)
    # a second liquidity venue (graduated pool) shows up as a contract receiving dev sells: detect by code
    cps = {r["to"] for r in recs if r["from"] == dev} | {r["from"] for r in recs if r["to"] == dev}
    kinds = onchain.codes(cl, list(cps)[:40])
    tr = [t for t in flows.trades(recs, pools, DEAD) if t["addr"] == dev]
    agg = flows.aggregate(tr).get(dev, {})
    # transfers to contracts that are not pools (lockers etc.) are listed separately
    to_contracts = sum(t["amount"] for t in tr if t["kind"] == "transfer_out" and kinds.get(t["counterparty"]) == "contract")
    bal = onchain.balances(cl, tok, [dev, L["market"]] + sorted(pools - {L["market"]}))
    pct = (lambda v: round(100 * v / supply, 2)) if supply else (lambda v: None)
    sold, out_w = agg.get("sold", 0), agg.get("transferred_out", 0) - to_contracts
    return {**L, "symbol": meta.get("symbol"), "name": meta.get("name"), "supply": supply / 10 ** dec,
            "dev_bought_pct": pct(agg.get("bought", 0)), "dev_sold_pct": pct(sold), "dev_transferred_to_wallets_pct": pct(out_w),
            "dev_transferred_to_contracts_pct": pct(to_contracts), "dev_burned_pct": pct(agg.get("burned", 0)),
            "dev_balance_pct": pct(bal.get(dev) or 0), "market_balance_pct": pct(bal.get(L["market"]) or 0),
            "pool_balance_pct": pct(sum(v or 0 for a, v in bal.items() if a in pools and a != L["market"])),
            "dev_token_txs": len(txs), "partial": partial, "sell_txs": agg.get("txs", []), "rug_score": round((pct(sold) or 0) + (pct(out_w) or 0), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--out", default=None)
    ns = ap.parse_args()
    cfg = C.load()
    cl = Client(cfg, None, budget_s=None)
    t0 = time.monotonic()
    head = cl.head()
    pad = cl.chain["launchpads"][0]
    lo = onchain.block_at_time(cl, head["block_time"] - ns.hours * 3600, head["block"], head["block_time"])
    launches = launch.enumerate(cl, pad, lo, head["block"])
    print(f"{len(launches)} launches in last {ns.hours}h (blocks {lo}-{head['block']}), {round(time.monotonic()-t0,1)}s", file=sys.stderr)
    launches = launches[-ns.limit:]
    rows = []
    for i, L in enumerate(launches):
        try:
            rows.append(scan_token(cl, L, head, cl.chain.get("known_pools", [])))
        except Exception as e:  # noqa: BLE001 - keep scanning
            rows.append({**L, "error": f"{type(e).__name__}: {str(e)[:80]}"})
        if i % 25 == 0:
            print(f"  {i}/{len(launches)} {round(time.monotonic()-t0,1)}s req={cl.requests}", file=sys.stderr)
    rows.sort(key=lambda r: -(r.get("rug_score") or 0))
    out = {"chain_id": cl.chain_id, "head": head, "hours": ns.hours, "launches": len(launches), "latency_s": round(time.monotonic() - t0, 1),
           "requests": cl.requests, "errors": cl.errors[:20], "rows": rows}
    path = ns.out or str(C.DATA / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}_pons_scan.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(f"saved {path}", file=sys.stderr)
    print(f"{'symbol':<12}{'dev_sold%':>10}{'dev_out%':>10}{'dev_bal%':>10}{'mkt%':>8}{'pool%':>8}  token / launcher")
    for r in rows[:40]:
        if r.get("error"):
            continue
        print(f"{(r['symbol'] or '?')[:11]:<12}{r['dev_sold_pct']:>10}{r['dev_transferred_to_wallets_pct']:>10}{r['dev_balance_pct']:>10}"
              f"{r['market_balance_pct']:>8}{r['pool_balance_pct']:>8}  {r['token']} / {r['launcher']}")
    cl.close()


if __name__ == "__main__":
    main()
