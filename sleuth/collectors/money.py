"""Follow the money (versioned, read-only, explorer-fed).

For each tracked wallet: which token disposals actually PAID OUT (native coin or an ERC-20 quote asset credited to the
wallet inside the same transaction), what left the wallet afterwards, and where each hop ended: a known bridge or
exchange (exit), another service, a contract, a dead address, a wallet that still holds, or an unresolved leaf when
depth, fan-out, budget or a revisit stopped the follow.

Rules this collector enforces (see docs/service/03-autonomy-and-reporting.md):
  * a disposal with NO quote asset credited in its tx is a TRANSFER, never a sale; and "nothing credited" is only
    asserted when the wallet's pages were read completely, otherwise the disposal is `unverified`;
  * proceeds are what the WALLET was credited, counted once per transaction (routing hops collapse);
  * only the credited assets are followed, after the first paid sale, and only up to the amount received (FIFO):
    value that leaves beyond the proceeds total is counted but not attributed to the sale;
  * "exit" (bridge / exchange) comes only from the configured known_services list, never from a label's wording;
    a busy unlabeled wallet is `high_volume` = unresolved;
  * amounts stay raw integer strings with decimals beside them (decimals None when the explorer gave none);
    timestamps are the explorer's UTC strings; a missing or malformed timestamp is a gap, not "time zero";
  * every leaf says why it stopped; any unresolved leaf, truncated page, failed label or skipped wallet turns the
    trail's verdicts to None (unknown), never "did not leave".
"""
import time
from datetime import UTC, datetime

from ..client import ApiError, BudgetExceeded
from ..labels import classify
from .types import COLLECTOR_VERSION

RULE_VERSION = "money-trail/0.2"
NATIVE = "native"
_BRIDGE_WORDS = ("bridge", "solver", "relay")
_EXCHANGE_WORDS = ("exchange", "hot wallet", "cex")
_PROVIDER_ERRORS = (ApiError, OSError, ValueError, TypeError, KeyError, AttributeError)
UNRESOLVED_STOPS = frozenset({"unresolved", "budget", "depth", "fanout", "high_volume", "revisited", "truncated"})
_ADDR_LEN = 42


def _h(o):
    """Lower-cased hash of an explorer {hash: ...} object; '' when the shape is wrong."""
    return (o.get("hash") or "").lower() if isinstance(o, dict) and isinstance(o.get("hash"), str) else ""


def _int(v):
    """Strict non-negative integer parse of an explorer numeric field; None when absent or malformed."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _ts(s):
    """ISO explorer timestamp -> unix seconds (UTC), or None when missing/malformed. Naive strings are taken as UTC."""
    if not isinstance(s, str) or not s:
        return None
    try:
        d = datetime.fromisoformat(s)   # Python 3.11+ accepts the trailing Z
    except ValueError:
        return None
    return d.replace(tzinfo=UTC).timestamp() if d.tzinfo is None else d.timestamp()


def _tok(x):
    t = x.get("token")
    return t if isinstance(t, dict) else {}


def _tok_addr(tok):
    """Blockscout names the key `address_hash` on recent versions and `address` on older ones."""
    a = tok.get("address_hash") or tok.get("address")
    return a.lower() if isinstance(a, str) and len(a) == _ADDR_LEN else ""


def _dec(tok):
    """Token decimals as given (0 is preserved); None when the explorer did not say."""
    return _int(tok.get("decimals"))


def _total(x):
    t = x.get("total")
    return _int(t.get("value")) if isinstance(t, dict) else None


class _Trail:
    """One collection run: shared label cache, gap list, visited set and budget flag, so a wallet that blew the
    budget leaves a gap and the remaining wallets still report."""

    def __init__(self, client, token, depth, fanout, max_pages):
        self.client, self.token = client, token.lower()
        self.depth, self.fanout, self.max_pages = depth, fanout, max_pages
        self.labels, self.gaps, self.complete, self.exhausted = {}, [], True, False
        self.expanded = set()   # addresses whose outbound pages were already read (no double fetch / double count)
        cfg = getattr(client, "cfg", None) or {}
        self.known = {k.lower(): v for k, v in (cfg.get("known_services") or {}).items()}

    def gap(self, kind, detail):
        self.complete = False
        self.gaps.append({"kind": kind, "detail": detail})

    def label(self, addr):
        """(kind, label) with kind in eoa|contract|service|bridge|exchange|high_volume|dead|unknown. Only a configured
        known service becomes bridge/exchange; a busy unlabeled EOA is high_volume (unresolved); a service contract stays `service`."""
        a = (addr or "").lower()
        if len(a) != _ADDR_LEN:
            return "unknown", "?"
        if a not in self.labels:
            try:
                meta = self.client.address(a)
                if not isinstance(meta, dict):
                    raise TypeError("address meta is not an object")
                cnt = self.client.counters(a) if not meta.get("is_contract") else None
                kind, lab = classify(meta, cnt, self.client.cfg)
            except BudgetExceeded:
                raise
            except _PROVIDER_ERRORS as e:
                self.gap("label", f"{a}: label lookup failed: {type(e).__name__}")
                kind, lab = "unknown", "label lookup failed"
            if kind == "service":
                known = (self.known.get(a) or "").lower()
                if a not in self.known:
                    # a name-marked service CONTRACT (router, pool, proxy) is a service; only a busy EOA is 'high_volume'
                    kind = "service" if meta.get("is_contract") else "high_volume"
                elif any(w in known for w in _BRIDGE_WORDS):
                    kind = "bridge"
                elif any(w in known for w in _EXCHANGE_WORDS):
                    kind = "exchange"
            self.labels[a] = (kind, lab)
        return self.labels[a]

    def pages(self, addr, what):
        """(items, complete) for an explorer list; incompleteness and malformed rows are recorded as gaps."""
        raw, ok = self.client.paginate(f"/addresses/{addr}/{what}", max_pages=self.max_pages)
        if not isinstance(raw, list):
            self.gap("provider", f"{what} for {addr}: unexpected response shape")
            return [], False
        items = [x for x in raw if isinstance(x, dict)]
        if len(items) != len(raw):
            self.gap("provider", f"{what} for {addr}: {len(raw) - len(items)} malformed row(s) skipped")
        if not ok:
            self.gap("truncated", f"{what} for {addr}: only the first {self.max_pages} page(s) read")
        return items, ok and len(items) == len(raw)

    # -- per wallet -------------------------------------------------------------------------------------------------
    def wallet(self, addr, role):
        a = addr.lower()
        g0 = len(self.gaps)
        kind, lab = self.label(a)
        w = {"addr": a, "role": role, "kind": kind, "label": lab, "disposals": [], "proceeds": {}, "hops": [],
             "endpoints": {}, "not_attributed": 0, "complete": True}
        if kind in ("high_volume", "bridge", "exchange"):
            # a bot / hot wallet with tens of thousands of txs: its pages are not this token's story and would eat the budget
            w["skipped"] = f"high-volume wallet not followed ({lab})"
            self.gap("skipped", f"{a}: {w['skipped']}")
            w["complete"] = False
            return w
        try:
            txs, ok1 = self.pages(a, "transactions")
            its, ok2 = self.pages(a, "internal-transactions")
            tts, ok3 = self.pages(a, "token-transfers")
        except BudgetExceeded:
            w["complete"] = False
            self.exhausted = True
            self.gap("budget", f"budget exhausted before {a} was fully read")
            return w
        pages_ok = ok1 and ok2 and ok3
        # credits to this wallet, grouped by tx: native internal transfers + native txs in + ERC-20 transfers in
        credits = {}
        for x in its:
            v = _int(x.get("value"))
            if _h(x.get("to")) == a and v and not x.get("error") and x.get("success", True) and isinstance(x.get("transaction_hash"), str):
                credits.setdefault(x["transaction_hash"].lower(), []).append({"asset": NATIVE, "symbol": "ETH", "raw": str(v), "decimals": 18, "from": _h(x.get("from"))})
        for x in txs:
            v = _int(x.get("value"))
            if _h(x.get("to")) == a and v and x.get("status") == "ok" and isinstance(x.get("hash"), str):
                credits.setdefault(x["hash"].lower(), []).append({"asset": NATIVE, "symbol": "ETH", "raw": str(v), "decimals": 18, "from": _h(x.get("from"))})
        for x in tts:
            tok = _tok(x)
            asset, v = _tok_addr(tok), _total(x)
            if (_h(x.get("to")) == a and asset and asset != self.token and tok.get("type") == "ERC-20" and v
                    and _h(x.get("from")) != a and isinstance(x.get("transaction_hash"), str)):
                credits.setdefault(x["transaction_hash"].lower(), []).append({"asset": asset, "symbol": tok.get("symbol") or "?", "raw": str(v),
                                                                             "decimals": _dec(tok), "from": _h(x.get("from"))})
        # disposals of THE token: paid (sale), unpaid (transfer / sink) or unverified (pages incomplete)
        first_paid, paid_txs, proceeds, rows = None, set(), {}, []
        for x in tts:
            tok = _tok(x)
            if _h(x.get("from")) != a or _tok_addr(tok) != self.token:
                continue
            t = _ts(x.get("timestamp"))
            if t is None or not isinstance(x.get("transaction_hash"), str):
                self.gap("malformed", f"{a}: token transfer without a usable timestamp/tx hash skipped")
                continue
            rows.append((t, x))
        for t, x in sorted(rows, key=lambda r: r[0]):
            tok = _tok(x)
            h = x["transaction_hash"].lower()
            paid = credits.get(h, [])
            to = _h(x.get("to"))
            tk, tl = self.label(to)
            if paid:
                cls = "sale"
            elif not pages_ok:
                cls = "unverified"
            elif tk in ("bridge", "exchange"):
                cls = "sent_to_bridge"
            elif tk == "contract":
                cls = "transfer_to_contract"
            elif tk == "service":
                cls = "transfer_to_service"
            else:
                cls = "transfer"
            amt = _total(x)
            w["disposals"].append({"ts": x.get("timestamp"), "tx": h, "to": to, "to_kind": tk, "to_label": tl,
                                   "raw": str(amt if amt is not None else 0), "decimals": _dec(tok),
                                   "paid": [{k: v for k, v in p.items() if k != "from"} for p in paid], "classification": cls})
            if paid:
                first_paid = t if first_paid is None else min(first_paid, t)
                if h not in paid_txs:   # a split disposal inside one receipt is credited once
                    paid_txs.add(h)
                    for p in paid:
                        slot = proceeds.setdefault(p["asset"], {"symbol": p["symbol"], "raw": 0, "decimals": p["decimals"]})
                        slot["raw"] += int(p["raw"])
        w["proceeds"] = {k: {**v, "raw": str(v["raw"])} for k, v in proceeds.items()}
        # outbound hops at/after the first paid sale, only the credited assets, only up to the amounts received
        if first_paid is not None:
            self.expanded.add(a)
            budgets = {k: v["raw"] for k, v in proceeds.items()}
            w["hops"], w["not_attributed"] = self.outbound(a, first_paid, txs, tts, budgets, paid_txs, depth=1, path=(a,))
        w["endpoints"] = self.summarise(w["hops"])
        w["complete"] = len(self.gaps) == g0
        return w

    def outbound(self, a, since, txs, tts, budgets, skip_txs, depth, path):
        """Outflows FROM `a` of the budgeted assets at/after `since`, FIFO-attributed up to each asset's budget.
        Returns (hops, not_attributed_count). EOA leaves within depth are read and recursed."""
        out = []
        for x in txs:
            v = _int(x.get("value"))
            if NATIVE in budgets and _h(x.get("from")) == a and v and x.get("status") == "ok" and isinstance(x.get("hash"), str):
                out.append({"ts": x.get("timestamp"), "tx": x["hash"].lower(), "from": a, "to": _h(x.get("to")), "asset": NATIVE, "symbol": "ETH",
                            "raw": str(v), "decimals": 18, "method": x.get("method")})
        for x in tts:
            tok = _tok(x)
            asset, v = _tok_addr(tok), _total(x)
            if asset in budgets and _h(x.get("from")) == a and v and isinstance(x.get("transaction_hash"), str):
                out.append({"ts": x.get("timestamp"), "tx": x["transaction_hash"].lower(), "from": a, "to": _h(x.get("to")), "asset": asset,
                            "symbol": tok.get("symbol") or "?", "raw": str(v), "decimals": _dec(tok), "method": None})
        keyed = []
        for h in out:
            t = _ts(h["ts"])
            if t is None:
                self.gap("malformed", f"{a}: outflow {h['tx']} without a usable timestamp skipped")
            elif t >= since and h["tx"] not in skip_txs:
                keyed.append((t, h))
        keyed.sort(key=lambda r: r[0])
        remaining, hops, not_attributed = dict(budgets), [], 0
        for _, h in keyed:
            left = remaining.get(h["asset"], 0)
            if left <= 0:
                not_attributed += 1   # beyond the proceeds total: counted, not followed
                continue
            take = min(int(h["raw"]), left)
            remaining[h["asset"]] = left - take
            h["attributed"] = str(take)
            hops.append(h)
        if len(hops) > self.fanout:
            self.gap("fanout", f"{a}: {len(hops)} outbound transfers after the sale, only the first {self.fanout} followed")
            hops, dropped = hops[:self.fanout], hops[self.fanout:]
        else:
            dropped = []
        for h in hops:
            h["depth"] = depth
            k, lab = self.label(h["to"])
            h["to_kind"], h["to_label"], h["children"] = k, lab, []
            if k == "eoa":
                if h["to"] in path or h["to"] in self.expanded:
                    h["stop"] = "revisited"
                    self.gap("cycle", f"{h['to']}: reached again at depth {depth}; its outflows were already attributed once")
                elif depth > self.depth or self.exhausted:   # recipients at level <= depth are read; deeper ones are labeled only
                    h["stop"] = "depth"
                    self.gap("depth", f"{h['to']}: wallet-to-wallet hop at depth {depth} not followed further")
                else:
                    try:
                        self.expanded.add(h["to"])
                        ntx, ok1 = self.pages(h["to"], "transactions")
                        ntt, ok2 = self.pages(h["to"], "token-transfers")
                        h["children"], _ = self.outbound(h["to"], _ts(h["ts"]), ntx, ntt, {h["asset"]: int(h["attributed"])}, set(), depth + 1, path + (h["to"],))
                        h["stop"] = "forwarded" if h["children"] else ("holding" if ok1 and ok2 else "truncated")
                    except BudgetExceeded:
                        h["stop"] = "budget"
                        self.exhausted = True
                        self.gap("budget", f"budget exhausted while following {h['to']}")
            elif k in ("bridge", "exchange"):
                h["stop"] = "exit"
            elif k in ("service", "dead", "contract", "high_volume"):
                h["stop"] = k
            else:
                h["stop"] = "unresolved"
        for d in dropped:
            d.update({"depth": depth, "to_kind": "unknown", "to_label": "not followed (fan-out cap)", "stop": "fanout", "children": []})
        return hops + dropped, not_attributed

    @staticmethod
    def summarise(hops):
        """Attributed totals by stop reason and asset: {stop: {asset: {symbol, raw, decimals}}}. A forwarding parent
        contributes what it kept (attributed minus what its children carried on) as `holding`."""
        tot = {}

        def add(stop, h, amount):
            slot = tot.setdefault(stop, {}).setdefault(h["asset"], {"symbol": h["symbol"], "raw": 0, "decimals": h["decimals"]})
            slot["raw"] += amount

        def walk(hs):
            for h in hs:
                if h.get("children"):
                    walk(h["children"])
                    kept = int(h["attributed"]) - sum(int(c["attributed"]) for c in h["children"])
                    if kept > 0:
                        add("holding", h, kept)
                else:
                    add(h["stop"], h, int(h["attributed"]))
        walk(hops)
        return {s: {a: {**v, "raw": str(v["raw"])} for a, v in d.items()} for s, d in tot.items()}


def money_trail(client, token, wallets, depth=2, fanout=6, max_pages=2):
    """wallets: [(addr, role)]. depth = how many wallet-to-wallet hops past the tracked wallet are READ (2 = the
    recipient and the recipient's recipient); the next level is labeled but not followed. Returns a JSON-able dict with
    per-wallet trails and three-valued verdicts: True on positive evidence; False only when every page read was
    complete, every label resolved and nothing was found; None whenever any gap exists."""
    t = _Trail(client, token, depth, fanout, max_pages)
    out = {"rule_version": RULE_VERSION, "collector_version": COLLECTOR_VERSION, "token": token.lower(), "wallets": [],
           "gaps": t.gaps, "complete": True, "observed_at": time.time()}
    for addr, role in wallets:
        if t.exhausted:
            t.gap("budget", f"budget exhausted; {addr} and any later wallets not read")
            break
        try:
            out["wallets"].append(t.wallet(addr, role))
        except BudgetExceeded:
            t.gap("budget", f"budget exhausted at {addr}; remaining wallets not read")
            break
        except _PROVIDER_ERRORS as e:
            t.gap("provider", f"{addr}: {type(e).__name__}: {str(e)[:120]}")
    out["complete"] = t.complete
    ws = out["wallets"]
    cls = [d["classification"] for w in ws for d in w["disposals"]]
    any_sale, any_sink, any_bridged = "sale" in cls, "transfer_to_contract" in cls, "sent_to_bridge" in cls
    leaves = {s for w in ws for s in w["endpoints"]}
    any_exit, any_left = "exit" in leaves, any(w["hops"] for w in ws)
    unresolved = bool(leaves & UNRESOLVED_STOPS)
    ok = t.complete and bool(ws)
    out["verdict"] = {
        "sale_proceeds_received": True if any_sale else (False if ok else None),
        "token_parked_in_non_venue_contract": True if any_sink else (False if ok else None),
        "token_sent_to_bridge": True if any_bridged else (False if ok else None),
        # the two proceeds questions have no answer at all when nothing was ever paid
        "proceeds_left_wallet": True if any_left else (False if ok and any_sale else None),
        "proceeds_reached_bridge_or_exchange": True if any_exit else (False if ok and any_sale and not unresolved else None),
    }
    return out
