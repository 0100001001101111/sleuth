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


def launchdump(out, client):
    if out.get("summary") and out.get("launch_dump") is None and not out.get("early_buyers"):
        return out["summary"]
    tz = client.chain.get("tz", "UTC")
    dec = (out.get("token_meta") or {}).get("decimals", 18)
    fmt = lambda v: f"{v / 10 ** dec:,.0f}"
    pct = lambda x: "?" if x is None else f"{100 * x:.1f}%"
    v = out.get("launch_dump")
    head = {True: "**LAUNCH DUMP: yes.**", False: "**Launch dump: not seen.**", None: "**Launch dump: unknown (incomplete).**"}[v]
    lines = [f"{head} First {out['first_blocks']} blocks: {len(out['early_buyers'])} buyers took {pct(out['share_bought'])} of supply "
             f"({fmt(out['early_total'])}); {pct(out['dumped_fraction'])} of that was sold inside the window "
             f"({fmt(out['sold_direct'])} directly, {fmt(out['sold_via_hub'])} via {len(out['hubs'])} hub contract(s))."]
    t = out.get("times") or {}
    if out.get("bundled"):
        lines.append(f"- **BUNDLED LAUNCH:** {out['bundle_wallets']} non-launcher wallets took {pct(out['bundle_share'])} of supply in the first "
                     f"{out['first_blocks']} blocks" + (" and had not sold inside the window (overhang)." if not v else "."))
    if out.get("first_sell_block") is not None:
        lines.append(f"- First sell {out['blocks_to_first_sell']} blocks after launch ({local(t.get('first_sell'), tz)}); "
                     f"hub sells: {out['hub_sell_txs']} txs from {out['hub_sellers']} distinct sender wallets.")
    for h in out["hubs"]:
        lines.append(f"- Hub {short(h['addr'])}: received {fmt(h['received'])} from {h['from_n']} early buyers [addr]({client.link('address', h['addr'])})")
    for b in out["early_buyers"][:8]:
        tag = " (launcher)" if b["is_launcher"] else ""
        lines.append(f"- Early buyer {short(b['addr'])}{tag}: {fmt(b['amount'])} ({b['pct_supply']}% supply), {b['kind']}")
    lines.append(f"- Window blocks {out['creation_block']}-{out['end_block']} ({local(t.get('creation'), tz)} -> {local(t.get('end'), tz)}); "
                 f"{out['coverage']['transfers']} transfers; {client.requests} requests.")
    for g in out.get("gaps") or []:
        lines.append(f"- gap: {g}")
    return "\n".join(lines)


def sol_triage(out, client):
    if out.get("summary") and not out.get("flow"):
        return out["summary"]
    tz = client.chain.get("tz", "UTC")
    cov, fl = out["coverage"], out["flow"]
    dev = out.get("dev_selling") or []
    head = ("**Dev-linked selling in window: " + ", ".join(f"{short(d['addr'])} sold {d['sold']:,.0f} ({d['pct_supply']}%)" for d in dev) + ".**") if dev else \
        ("**Dev-linked selling in window: none.**" if (out.get("launch") or {}).get("launcher") else "**Dev-linked selling: launcher unknown (pass --dev).**")
    lines = [head + (f" launcher {short(out['launch']['launcher'])}" if (out.get("launch") or {}).get("launcher") else "")]
    lines.append(f"- Window {local(cov['from_time'], tz)} -> {local(cov['to_time'], tz)}: sold {fl['sold']:,.0f} by {fl['sellers']} wallets / "
                 f"bought {fl['bought']:,.0f} by {fl['buyers']}; {cov['txs']} txs read; pools {len(cov['pools'])}.")
    for s_ in out.get("top_sellers") or []:
        lines.append(f"- Seller {short(s_['addr'])}{' (dev-linked)' if s_['dev_linked'] else ''}: {s_['sold']:,.0f} ({s_['pct_supply']}% supply), "
                     f"{len(s_['sell_txs'])} sell txs, +{s_['sol_proceeds']} SOL, bought in window {s_['bought_in_window']:,.0f}")
    auth = out.get("authorities") or {}
    lines.append(f"- Mint authority {'SET ' + short(auth['mint']) if auth.get('mint') else 'revoked'}; freeze authority "
                 f"{'SET ' + short(auth['freeze']) if auth.get('freeze') else 'revoked'}; top-10 wallet holders "
                 f"{str(out.get('top10_wallet_pct')) + '%' if out.get('holders') else 'unavailable'} of supply.")
    for h in (out.get("holders") or [])[:5]:
        lines.append(f"- Holder {short(h['owner'] or h['token_account'])}: {h['pct_supply']}% ({h['kind']})")
    lines.append(f"- {client.requests} requests, {out['timing'].get('total_s')}s; coverage complete: {cov['complete']}.")
    for g in out.get("gaps") or []:
        lines.append(f"- gap: {g}")
    return "\n".join(lines)


def sol_mint(out, client):
    if out.get("summary") and not out.get("holders"):
        return out["summary"]
    m = out["token_meta"]
    lines = [f"**Mint {short(out['token'])}**: supply {m['total_supply'] / 10 ** m['decimals']:,.0f}, decimals {m['decimals']}; "
             f"flags: {', '.join(out['flags']) or 'none (authorities revoked, classic token program)'}."]
    lines.append(f"- Top-10 wallet holders {out['top10_wallet_pct']}% of supply; bonding curve {short(out['bonding_curve'])}.")
    for h in out["holders"][:10]:
        lines.append(f"- {short(h['owner'] or h['token_account'])}: {h['pct_supply']}% ({h['label'] or h['kind']}) [acct]({client.link('account', h['owner'] or h['token_account'])})")
    for g in out.get("gaps") or []:
        lines.append(f"- gap: {g}")
    return "\n".join(lines)
