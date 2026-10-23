"""Concise human summaries (<=150 words for urgent answers) with evidence links."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def local(ts, tz):
    if not ts:
        return "?"
    if isinstance(ts, (int, float)):
        d = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d.astimezone(ZoneInfo(tz)).strftime("%b %d %I:%M:%S %p %Z")


def short(a):
    return a[:6] + "…" + a[-4:] if a and len(a) > 12 else (a or "?")


def triage(out, client):
    tz = client.chain.get("tz", "UTC")
    if "summary" in out:
        return out["summary"]
    tok = out["token_meta"]
    sym = tok.get("symbol")
    L = []
    roles = lambda t: set(t.get("roles") or [t.get("role")])
    dev_rows = [t for t in out["tracked"] if roles(t) & {"launcher", "dev"}]
    recip_rows = [t for t in out["tracked"] if "initial_recipient" in roles(t) and not roles(t) & {"launcher", "dev"}]
    dev_sold = [t for t in dev_rows if t["sold"]]
    verdict = out.get("dev_sold_in_window")
    if verdict is True or dev_sold:
        L.append("**DEV SELLING IN WINDOW: YES.** " + "; ".join(
            f"{t['role']} {short(t['addr'])} sold {t['sold']:,.0f} {sym}" + (f" [tx]({client.link('tx', t['sell_txs'][0])})" if t['sell_txs'] else "") for t in dev_sold))
    elif verdict is False:
        L.append("**Dev selling in window: none.** " + "; ".join(
            f"{t['role']} {short(t['addr'])} holds " + (f"{t['balance_now']:,.0f} ({t['balance_pct']}%)" if t['balance_fresh'] else "? (balance fetch failed)")
            + (f", sent out {t['transferred_out']:,.0f}" if t['transferred_out'] else "") for t in dev_rows[:4])
            + " (window only — run `sleuth dev-history` for the full-life check)")
    elif not out.get("dev_identified", bool(dev_rows)):
        L.append("**Dev selling in window: UNKNOWN — developer not identified.** Run `sleuth dev-history`, or pass --dev.")
    else:
        L.append("**Dev selling in window: UNKNOWN (coverage incomplete — see gaps).** " + "; ".join(
            f"{t['role']} {short(t['addr'])} holds " + (f"{t['balance_now']:,.0f} ({t['balance_pct']}%)" if t['balance_fresh'] else "?")
            for t in dev_rows[:4]))
    fc = out.get("dev_fee_claims")
    if fc and fc.get("n"):  # n, not eth: a dust claim must not round away into silence
        srcs = ", ".join(fc["sources"].values()) or "fee escrow"
        eth = fc["eth"] if fc["eth"] else "<0.0001"
        L.append(f"- **Dev extracting via creator-fee claims: {eth} ETH** over {fc['n']} claim(s) from {srcs}; "
                 f"latest {local(fc['latest'], tz)}" + (" (first page only)" if fc.get("partial") else ""))
    lf = out.get("launcher_funding")
    for s in (lf or {}).get("signals") or []:
        L.append(f"- Launch-farm signal: {s}")
    recip_sold = [t for t in recip_rows if t["sold"]]
    if recip_sold:
        L.append("- Launch-tx recipient selling (bundled buyer possible — NOT proof of dev): " + "; ".join(
            f"{short(t['addr'])} sold {t['sold']:,.0f} {sym}" + (f" [tx]({client.link('tx', t['sell_txs'][0])})" if t['sell_txs'] else "") for t in recip_sold))
    f, cov = out["flow"], out["coverage"]
    L.append(f"- Window {local(cov['from_time'], tz)} → {local(cov['to_time'], tz)} (blocks {cov['from_block']}-{cov['to_block']}): "
             f"sold {f['sold']:,.0f} by {f['sellers']} wallets / bought {f['bought']:,.0f} by {f['buyers']} {sym}"
             + (f" / burned {f['burned']:,.0f}" if f['burned'] else "") + f"; {cov['txs']} txs.")
    for s in out["top_sellers"][:3]:
        kinds = s.get("acquired_from_kinds") or {}
        src = ", ".join((f"{kinds.get(k, 'addr')} {short(k)}" if k.startswith("0x") else k) + f" {v:,.0f}" for k, v in (s.get("acquired_from") or {}).items())
        fund = s.get("eth_funders_internal")
        pre = [f for f in (fund or []) if f.get("pre_acquisition")]
        pick = pre[0] if pre else (fund[0] if fund else None)
        fl = (f"; ETH {'pre-buy ' if pre else ''}from {short(pick['from'])} ({pick['label'] or 'wallet'})" if pick
              else ("; ETH funding unchecked" if fund is None else "; no internal ETH funding found"))
        L.append(f"- Seller {short(s['addr'])}{' [contract]' if s['kind'] == 'contract' else ''}: {s['sold']:,.0f} ({s['pct_supply']}% supply); "
                 f"acquired via {src or '?'}{fl}" + ("; **DEV-LINKED**" if s.get("dev_linked") else "")
                 + (f" [tx]({client.link('tx', s['sell_txs'][0])})" if s["sell_txs"] else ""))
    for b in out.get("burns", [])[:2]:
        L.append(f"- Burn: {b['amount']:,.0f} {sym} by {short(b['addr'])} at {local(b['ts'], tz)} [tx]({client.link('tx', b['tx'])})")
    L.append(f"- Checked through block {out['head']['block']} ({local(out['head']['block_time'], tz)}); {out['requests']} requests, {out['latency_s']}s"
             + (" (timestamps interpolated)" if cov.get("timestamps_approx") else "") + ".")
    for g in out["gaps"][:3]:
        L.append(f"- Gap: {g}")
    return "\n".join(L)


def trace(out, client):
    v = out["verdict"]
    dec = 10 ** (out.get("token_meta") or {}).get("decimals", 18)
    L = []
    yn = lambda x, no="no": "YES" if x else (no if x is False else "unknown (incomplete)")
    heads = ["direct tx link: " + yn(v["direct_tx_connection"]), "shared funder: " + yn(v["shared_funding"]),
             "coordinated buys: " + yn(v["coordinated_trading"]), "dev connection: " + yn(v["dev_connection"], "none found")]
    L.append("**" + " | ".join(heads) + ".** Ownership: unknown.")
    for d in out["direct"][:4]:
        what = f"{d.get('eth'):.4f} ETH" if d.get("eth") is not None else f"{d.get('token_amount', 0)/dec:,.0f} tokens"
        L.append(f"- {short(d['from'])} → {short(d['to'])}: {what}" + (f" via {d['via']}" if d.get("via") else "") + f" [tx]({client.link('tx', d['tx'])})")
    for c in out["coordinated"][:2]:
        L.append(f"- Batch by {short(c['sender'])} funded {len(c['siblings'])} wallets ({', '.join(f'{a:.6f}' for a in c['amounts_eth'][:3])} ETH); "
                 f"same-block buys: {', '.join(str(b) for b in c['same_block_buys']) or 'none'} [tx]({client.link('tx', c['batch_tx'])})")
    for s in out["shared_funders"][:3]:
        L.append(f"- Shared funder {short(s['funder'])} ({s['label']}) → {len(s['members'])} wallets")
    for s in v["shared_service_only"][:2]:
        L.append(f"- Shared SERVICE only ({s['label']}): {len(s['members'])} wallets. Not ownership evidence.")
    for b in (v.get("bot_controller") or [])[:2]:
        L.append(f"- BOT-CONTROLLER pattern: unnamed contract {short(b['contract'])} funded {len(b['members'])} "
                 "wallet(s) pre-buy AND received ETH back after their sells — two-way flow consistent with one "
                 "operator cycling wallets (coordination signal; not ownership proof)")
    for w in out["wallets"]:
        f = w.get("funding_before_acquisition")
        sells = [x for x in w["token_disposals"] if x["via"] == "pool (sell)"]
        sold = sum(x["amount"] for x in sells) / dec
        gross = w.get("gross_sale_proceeds_eth")
        pre = f"pre-buy funding {f['eth']:.4f} ETH from {short(f['from'])} ({f['from_label']})" if f else "no pre-buy funding found"
        dest = ", ".join(f"{short(p['to'])} ({p['to_label']})" for p in w["proceeds_out"][:2]) or "unresolved/none yet"
        L.append(f"- {short(w['addr'])} [{w['label']}]: sold {sold:,.0f} (gross {gross} ETH); {pre}; ETH out after → {dest}")
    for l in out["dev_links"][:3]:
        L.append(f"- DEV LINK: {l['type']} {short(l['addr'])} [tx]({client.link('tx', l.get('tx', ''))})")
    L.append(f"- {out['requests']} requests, {out['latency_s']}s." + (" Gaps: " + "; ".join(out["gaps"]) if out["gaps"] else ""))
    return "\n".join(L)


def burn(out, client):
    tz = client.chain.get("tz", "UTC")
    if not out.get("found"):
        return f"tx {out['tx']} not found on chain {client.chain_id}."
    if not out["burns"]:
        return f"**No burn in tx.** method={out['method']} status={out['status']} [tx]({client.link('tx', out['tx'])})"
    b = out["burns"][0]
    amt = b["amount"] / 10 ** b["decimals"]
    via = " via internal/delegated execution" if out["executed_via_internal_call"] or out.get("sender_delegated_eip7702") else ""
    return (f"**Burn confirmed: {amt:,.0f} {b['symbol']}** from {short(b['from'])} to {short(b['to'])}{via} at {local(out['ts'], tz)} "
            f"(block {out['block']}). Type: {out.get('burn_type')}. [tx]({client.link('tx', out['tx'])})")


def dev_history(out, client):
    if "summary" in out:
        return out["summary"]
    sym = (out.get("token_meta") or {}).get("symbol") or "tokens"
    v = out.get("dev_ever_sold")
    L = []
    if v is True:
        n = sum(len(w["swap_confirmed_sells"]) for w in out["wallets"])
        L.append(f"**DEV HAS SOLD: YES** — {n} swap-confirmed sell tx(s) over the token's full life.")
    elif v is False:
        L.append("**Dev has NEVER sold** (full history read; every outflow accounted for, no swap-confirmed sale).")
    elif not out.get("dev_wallets"):
        L.append("**Dev selling: UNKNOWN — developer not identified.** Pass --dev if you know the wallet.")
    else:
        L.append("**Dev selling: UNKNOWN (history incomplete or ambiguous venue outflows — see gaps).**")
    for w in out["wallets"]:
        t = w.get("totals") or {}
        L.append(f"- {short(w['addr'])}: sold(confirmed) {t.get('sold_confirmed', 0):,.0f}, venue-outflow(ambiguous) "
                 f"{t.get('venue_outflow', 0):,.0f}, burned {t.get('burned', 0):,.0f}, bought {t.get('bought', 0):,.0f}, "
                 f"transferred out {t.get('transferred_out', 0):,.0f} {sym}; holds "
                 + (f"{w['balance_now']:,.0f}" if w.get("balance_now") is not None else "?")
                 + ("" if w.get("complete") else " (history PARTIAL)"))
        for h in (w.get("swap_confirmed_sells") or [])[:2]:
            L.append(f"  - sell [tx]({client.link('tx', h)})")
        for h in (w.get("unconfirmed_venue_outflows") or [])[:2]:
            L.append(f"  - ambiguous venue outflow [tx]({client.link('tx', h)})")
        fc = w.get("fee_claims")
        if fc and fc.get("n"):
            srcs = ", ".join(fc["sources"].values()) or "fee escrow"
            eth = fc["eth"] if fc["eth"] else "<0.0001"
            L.append(f"  - **creator-fee claims: {eth} ETH** over {fc['n']} claim(s) from {srcs}"
                     + (" (first page only)" if fc.get("partial") else ""))
    L.append(f"- {out['requests']} requests, {out['latency_s']}s." + (" Gaps: " + "; ".join(out["gaps"][:4]) if out["gaps"] else ""))
    return "\n".join(L)


def txdecode(out, client):
    tz = client.chain.get("tz", "UTC")
    if not out.get("found"):
        return f"tx {out['tx']} not found on chain {client.chain_id}: " + "; ".join(out.get("gaps") or [])
    sw = out["swap_evidence"]
    L = [f"**tx {short(out['tx'])}** status={out['status']} block={out['block']} ({local(out['ts'], tz)}) "
         f"signer={short(out['signer'])} → {short(out['to']) if out['to'] else 'contract creation'}"
         + (f", {out['eth_value']} ETH" if out.get("eth_value") else "")]
    L.append("- Swap event: " + (f"YES (emitted by {', '.join(short(a) for a in sw['emitting_contracts'][:3])})" if sw["found"] else "none decoded"))
    for tk, nets in list(out["nets"].items())[:6]:
        sym = (out["tokens"].get(tk) or {}).get("symbol") or short(tk)
        moves = ", ".join(f"{short(a)} ({out['labels'].get(a, '?')}) {v:+,.2f}" for a, v in list(nets.items())[:6])
        L.append(f"- {sym}: {moves}")
    if out.get("internal_eth"):
        L.append("- Internal ETH: " + ", ".join(f"{short(x['from'])}→{short(x['to'])} {x['eth']}" for x in out["internal_eth"][:4]))
    if out["gaps"]:
        L.append("- Gaps: " + "; ".join(out["gaps"][:4]))
    L.append(f"- [tx]({client.link('tx', out['tx'])})")
    return "\n".join(L)


def contract(out, client):
    L = [f"**{out.get('name') or 'contract'} {short(out['addr'])}**: verified={out['verified']} proxy={out['proxy_type']} creator={short(out.get('creator'))}"]
    if out["flags"]:
        L.append("- Flags: " + "; ".join(out["flags"]))
    if out.get("privileged_functions"):
        L.append("- Privileged fns: " + ", ".join(out["privileged_functions"][:12]))
    for c in out.get("recent_privileged_calls", [])[:4]:
        L.append(f"- {c['method']} by {short(c['from'])} at {local(c['ts'], client.chain.get('tz','UTC'))} [tx]({client.link('tx', c['tx'])})")
    L.append(f"- [contract]({client.link('address', out['addr'])}?tab=contract)")
    return "\n".join(L)
