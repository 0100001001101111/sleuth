"""Solana support on the public RPC only (no keys, no indexer). Added for the hedge fund 2026-10-21.

Solana has no event logs: SPL balances live in per-owner token accounts, so every trade is read from a transaction's
pre/post token balances (netted per OWNER, exactly like the EVM per-tx netting) and turned into synthetic transfer
records that the existing flows/launchdump code consumes unchanged. Pools are accounts owned by an AMM program
(pump.fun bonding curve PDA, PumpSwap, Raydium) plus two-way repeat counterparties; wallets are System-owned accounts.

Coverage: signatures come from the MINT account. pump.fun create/buy/sell, PumpSwap and Raydium swaps reference the
mint, so curve-phase and AMM flows are complete; a plain wallet-to-wallet SPL transfer may not reference the mint and
is reported as a gap, never as "no activity". The public node limits ~100 requests per 10 s: the client paces itself,
backs off on 429, and the time budget turns a slow run into an honest partial result.
"""
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict

from . import config as C, flows
from .client import BudgetExceeded, Cache

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SYSTEM = "11111111111111111111111111111111"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"
DEAD = {SYSTEM, INCINERATOR}
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnwqTqoP3JF1t"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
AMM_PROGRAMS = {PUMP: "pump.fun bonding curve", PUMPSWAP: "PumpSwap AMM",
                "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
                "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
                "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM"}
RAYDIUM_V4_AUTHORITY = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
LAMPORTS = 1_000_000_000


# ---------- base58 + program-derived addresses (pure Python, no dependency) ----------
def b58decode(s):
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + body


def b58encode(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out


_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def on_curve(b):
    """True if the 32 bytes decompress to a point on ed25519 (a PDA must NOT)."""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    x2 = (y2 - 1) * pow((_D * y2 + 1) % _P, _P - 2, _P) % _P
    if x2 == 0:
        return (b[31] >> 7) == 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * pow(2, (_P - 1) // 4, _P) % _P
        if (x * x - x2) % _P:
            return False
    return True


def find_program_address(seeds, program_id):
    pid = b58decode(program_id)
    for nonce in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([nonce]) + pid + b"ProgramDerivedAddress").digest()
        if not on_curve(h):
            return b58encode(h), nonce
    raise ValueError("no program address")


def bonding_curve(mint):
    """pump.fun bonding-curve account for a mint (seeds: 'bonding-curve', mint)."""
    return find_program_address([b"bonding-curve", b58decode(mint)], PUMP)[0]


# ---------- client ----------
class SolClient:
    chain_id = "solana"

    def __init__(self, cfg=None, budget_s=None, cache=True):
        self.cfg = cfg or C.load()
        self.chain = self.cfg["chains"]["solana"]
        self.rpc_url, self.explorer = self.chain["rpc"], self.chain["explorer"].rstrip("/")
        self.deadline = time.monotonic() + budget_s if budget_s else None
        self.requests, self.errors = 0, []
        self.cache = Cache(C.DATA / "cache.db") if cache else None
        self.rate = float(self.chain.get("rpc_rate", 8.0))
        self.burst = int(self.chain.get("rpc_burst", 20))
        self._tokens, self._at, self._lock = float(self.burst), time.monotonic(), threading.Lock()

    def remaining(self):
        return None if self.deadline is None else self.deadline - time.monotonic()

    def _throttle(self, n):
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
            self._at = now
            if self._tokens < n:
                time.sleep((n - self._tokens) / self.rate)
                self._tokens, self._at = 0.0, time.monotonic()
            else:
                self._tokens -= n

    def _post(self, body, url=None):
        req = urllib.request.Request(url or self.rpc_url, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.cfg["http"]["timeout"]) as r:
            return json.load(r)

    def rpc(self, calls, batch=20):
        """[(method, params)] -> results aligned with calls; None where the node answered with an error (recorded).
        The public node answers HTTP 200 with a JSON-body 429 ("Too many requests for a specific RPC call") per item:
        those items are retried with a growing pause; anything else is recorded once."""
        out = [None] * len(calls)
        for i in range(0, len(calls), batch):
            pending = list(range(i, min(i + batch, len(calls))))
            for attempt in range(5):
                if not pending:
                    break
                if self.deadline and time.monotonic() > self.deadline:
                    raise BudgetExceeded()
                self._throttle(len(pending))
                body = [{"jsonrpc": "2.0", "id": k, "method": calls[k][0], "params": calls[k][1]} for k in pending]
                res = None
                url = self.rpc_url
                fallbacks = self.chain.get("rpc_fallbacks") or []
                if attempt >= 1 and fallbacks:
                    url = fallbacks[(attempt - 1) % len(fallbacks)]  # a second public node for items the first one throttles
                try:
                    res = self._post(body, url)
                    self.requests += 1
                except urllib.error.HTTPError as e:
                    self.requests += 1
                    if e.code != 429:
                        self.errors.append(f"HTTP {e.code} on {calls[pending[0]][0]}")
                        break
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    self.errors.append(f"{type(e).__name__}: {e}")
                if isinstance(res, dict):
                    res = [dict(res, id=k) for k in pending] if "error" in res else [res]
                if not isinstance(res, list):
                    time.sleep(2.0 * (attempt + 1))
                    continue
                by = {r.get("id"): r for r in res if isinstance(r, dict)}
                still = []
                for k in pending:
                    r = by.get(k)
                    err = (r or {}).get("error") if r else None
                    if r is None or (err and err.get("code") == 429):
                        still.append(k)
                    elif err:
                        self.errors.append(f"rpc {calls[k][0]}: {err}")
                    else:
                        out[k] = r.get("result")
                pending = still
                if pending:
                    time.sleep(2.0 * (attempt + 1))
            for k in pending:
                self.errors.append(f"rpc {calls[k][0]}: still rate-limited after retries")
        return out

    def head(self):
        (slot,) = self.rpc([("getSlot", [{"commitment": "confirmed"}])])
        (bt,) = self.rpc([("getBlockTime", [slot])]) if slot else (None,)
        return {"block": slot, "block_time": bt, "chain_id": "solana"}

    def link(self, kind, value):
        return f"{self.explorer}/{'tx' if kind == 'tx' else 'account'}/{value}"

    def close(self):
        pass


# ---------- facts ----------
def mint_info(client, mint):
    (acc,) = client.rpc([("getAccountInfo", [mint, {"encoding": "jsonParsed"}])])
    v = (acc or {}).get("value") or {}
    info = ((v.get("data") or {}).get("parsed") or {}).get("info") or {}
    if not info or "decimals" not in info:
        return None
    return {"mint": mint, "decimals": info["decimals"], "total_supply": int(info.get("supply") or 0),
            "mint_authority": info.get("mintAuthority"), "freeze_authority": info.get("freezeAuthority"),
            "token_program": v.get("owner"), "symbol": None, "name": None}


def kinds(client, addrs):
    """{addr: eoa|contract|dead}. contract = account owned by a program other than System (PDA, pool, program)."""
    out, need = {}, []
    for a in dict.fromkeys(a for a in addrs if a):
        if a in DEAD:
            out[a] = "dead"
            continue
        v, _ = client.cache.get(f"solkind:{a}") if client.cache else (None, None)
        if v:
            out[a] = v
        else:
            need.append(a)
    for i in range(0, len(need), 100):
        chunk = need[i:i + 100]
        (res,) = client.rpc([("getMultipleAccounts", [chunk, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])])
        vals = (res or {}).get("value") or [None] * len(chunk)
        for a, v in zip(chunk, vals):
            k = "eoa" if v is None or v.get("owner") == SYSTEM else "contract"
            out[a] = k
            if client.cache and v is not None:
                client.cache.put(f"solkind:{a}", k, "solana")
    return out


def token_account_owners(client, accts):
    out = {}
    for i in range(0, len(accts), 100):
        chunk = accts[i:i + 100]
        (res,) = client.rpc([("getMultipleAccounts", [chunk, {"encoding": "jsonParsed"}])])
        for a, v in zip(chunk, (res or {}).get("value") or []):
            info = (((v or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            out[a] = info.get("owner")
    return out


def largest_holders(client, mint, tries=3, pause=8.0):
    """Top-20 token accounts. The public node throttles this method hard; wait and retry a few times inside the budget."""
    vals = []
    for i in range(tries):
        (res,) = client.rpc([("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])])
        vals = (res or {}).get("value") or []
        if vals or i == tries - 1 or (client.remaining() is not None and client.remaining() < pause + 5):
            break
        time.sleep(pause)
    owners = token_account_owners(client, [x["address"] for x in vals])
    return [{"token_account": x["address"], "owner": owners.get(x["address"]), "amount": int(x["amount"])} for x in vals]


def signatures(client, address, since_time=None, max_pages=60):
    """Newest-first pages of getSignaturesForAddress. Stops at a short page (history exhausted -> complete) or when the
    page's oldest blockTime is before since_time. Returns (rows, complete)."""
    out, before, complete = [], None, False
    for _ in range(max_pages):
        (res,) = client.rpc([("getSignaturesForAddress", [address, {"limit": 1000, "before": before, "commitment": "confirmed"}])])
        if res is None:
            break
        out.extend(res)
        if len(res) < 1000:
            complete = True
            break
        if since_time is not None and res[-1].get("blockTime") and res[-1]["blockTime"] < since_time:
            complete = True
            break
        before = res[-1]["signature"]
    return out, complete


def fetch_txs(client, sigs):
    """{signature: parsed tx} via batched getTransaction; confirmed txs are immutable so they are cached."""
    out, need = {}, []
    for s in dict.fromkeys(sigs):
        v, _ = client.cache.get(f"soltx:{s}") if client.cache else (None, None)
        if v:
            out[s] = v
        else:
            need.append(s)
    calls = [("getTransaction", [s, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}]) for s in need]
    for s, tx in zip(need, client.rpc(calls, batch=5) if calls else []):
        if isinstance(tx, dict):
            out[s] = tx
            if client.cache:
                client.cache.put(f"soltx:{s}", tx, "solana", tx.get("slot"))
    return out


def fee_payer(tx):
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    for k in keys:
        if isinstance(k, dict) and k.get("signer"):
            return k.get("pubkey")
    return keys[0].get("pubkey") if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)


def programs_in(tx):
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return {k.get("pubkey") if isinstance(k, dict) else k for k in keys}


def tx_records(sig, tx, mint):
    """Net SPL delta per OWNER for `mint` from pre/post token balances -> synthetic transfer records (net senders ->
    net receivers, greedy pairing; a mint shows as SYSTEM -> receiver, a burn as sender -> INCINERATOR), plus the SOL
    (lamport) delta per account. A failed tx (meta.err) yields nothing."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return [], {}
    pre, post = defaultdict(int), defaultdict(int)
    for b in meta.get("preTokenBalances") or []:
        if b.get("mint") == mint:
            pre[b.get("owner") or f"acct:{b['accountIndex']}"] += int(b["uiTokenAmount"]["amount"])
    for b in meta.get("postTokenBalances") or []:
        if b.get("mint") == mint:
            post[b.get("owner") or f"acct:{b['accountIndex']}"] += int(b["uiTokenAmount"]["amount"])
    delta = {o: post[o] - pre[o] for o in set(pre) | set(post) if post[o] - pre[o]}
    neg = sorted([[o, -v] for o, v in delta.items() if v < 0], key=lambda x: -x[1])
    pos = sorted([[o, v] for o, v in delta.items() if v > 0], key=lambda x: -x[1])
    tn, tp = sum(v for _, v in neg), sum(v for _, v in pos)
    if tp > tn:
        neg.append([SYSTEM, tp - tn])
    elif tn > tp:
        pos.append([INCINERATOR, tn - tp])
    recs, i, j, idx, slot = [], 0, 0, 0, tx.get("slot")
    while i < len(neg) and j < len(pos):
        v = min(neg[i][1], pos[j][1])
        if v > 0:
            recs.append({"tx": sig, "block": slot, "idx": idx, "from": neg[i][0], "to": pos[j][0], "value": v})
            idx += 1
        neg[i][1] -= v
        pos[j][1] -= v
        if neg[i][1] == 0:
            i += 1
        if pos[j][1] == 0:
            j += 1
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
    sol = {k: b - a for k, a, b in zip(keys, meta.get("preBalances") or [], meta.get("postBalances") or []) if a != b}
    return recs, sol


def records_for(client, mint, rows):
    """rows: signature rows (any order). -> (records, sol_by_tx, txs, missing)"""
    txs = fetch_txs(client, [r["signature"] for r in rows])
    records, sol, missing = [], {}, []
    for r in rows:
        tx = txs.get(r["signature"])
        if not tx:
            missing.append(r["signature"])
            continue
        recs, s = tx_records(r["signature"], tx, mint)
        records.extend(recs)
        sol[r["signature"]] = s
    return records, sol, txs, missing


def launch_info(client, mint, max_pages=80):
    """Creation = the oldest signature on the mint; launcher = its fee payer; market = the pump.fun curve if it exists.
    Returns (info or None, all signature rows newest-first, history_complete)."""
    curve = bonding_curve(mint)
    market = curve if kinds(client, [curve]).get(curve) == "contract" else None
    # The curve account is referenced by the create tx and every curve-phase trade, and its history ends at graduation,
    # so paging it reaches the creation far sooner than paging the mint (which every later AMM swap references too).
    sigs, complete = signatures(client, market or mint, max_pages=max_pages)
    if not sigs:
        return None, [], complete
    oldest = sigs[-1]
    tx = fetch_txs(client, [oldest["signature"]]).get(oldest["signature"]) or {}
    info = {"creation_tx": oldest["signature"], "creation_block": oldest["slot"], "creation_time": oldest.get("blockTime"),
            "launcher": fee_payer(tx) if tx else None, "market": market,
            "launchpad": "pump.fun" if PUMP in programs_in(tx) else None, "initial_recipients": [], "history_complete": complete}
    return info, sigs, complete


def owner_programs(client, addrs):
    """{addr: owning program} for accounts that exist (cached). An account owned by an AMM program is a venue."""
    out, need = {}, []
    for a in dict.fromkeys(a for a in addrs if a and a not in DEAD):
        v, _ = client.cache.get(f"solowner:{a}") if client.cache else (None, None)
        if v:
            out[a] = v
        else:
            need.append(a)
    for i in range(0, len(need), 100):
        chunk = need[i:i + 100]
        (res,) = client.rpc([("getMultipleAccounts", [chunk, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])])
        for a, v in zip(chunk, (res or {}).get("value") or [None] * len(chunk)):
            if v and v.get("owner"):
                out[a] = v["owner"]
                if client.cache:
                    client.cache.put(f"solowner:{a}", v["owner"], "solana")
    return out


def pools_for(client, records, market):
    owners = {r["from"] for r in records} | {r["to"] for r in records}
    progs = owner_programs(client, [o for o in owners if o not in DEAD])
    kd = {a: ("contract" if prog != SYSTEM else "eoa") for a, prog in progs.items()}
    kd.update({o: "eoa" for o in owners if o not in kd and o not in DEAD})
    known = {p for p in client.chain.get("known_pools", [])} | ({market} if market else set())
    known |= {a for a, prog in progs.items() if prog in AMM_PROGRAMS}  # curve, PumpSwap pool, Raydium vault authority
    pools, hits, _ = flows.detect_pools(records, lambda ad: {a: kd.get(a, "eoa") for a in ad}, known)
    return pools, hits, kd


# ---------- commands ----------
def _slot_time(rows, slot):
    for r in rows:
        if r["slot"] == slot and r.get("blockTime"):
            return r["blockTime"]
    return None


def launchdump(client, mint, blocks=1500, first_blocks=3, max_txs=200):
    from . import launchdump as LD
    out = {"stage": "launchdump", "chain_id": "solana", "token": mint, "gaps": [], "head": client.head()}
    meta = mint_info(client, mint)
    out["token_meta"] = meta or {}
    if not meta:
        out["summary"] = f"{mint} is not an SPL mint on Solana mainnet"
        return out
    li, sigs, complete = launch_info(client, mint)
    if not li:
        out["summary"] = "no signatures found for this mint"
        return out
    out["launch"] = li
    c, end = li["creation_block"], li["creation_block"] + blocks
    window = sorted((s for s in sigs if s["slot"] <= end and not s.get("err")), key=lambda s: s["slot"])
    truncated = len(window) > max_txs
    window = window[:max_txs]
    records, sol, txs, missing = records_for(client, mint, window)
    pools, _, kd = pools_for(client, records, li["market"])
    senders = lambda hs: {h: fee_payer(txs[h]) for h in hs if h in txs}
    minted = sum(r["value"] for r in records if r["from"] == SYSTEM)
    supply_at_launch = max(meta["total_supply"] or 0, minted) or None  # burns since launch shrink the current supply
    if minted and meta["total_supply"] and minted > meta["total_supply"]:
        out["gaps"].append(f"supply at launch {minted} > current supply {meta['total_supply']} (burned since); shares use the launch supply")
    res = LD.analyze(records, li["market"], li["launcher"], c, supply_at_launch,
                     lambda ad: {a: kd.get(a, "eoa") for a in ad}, senders, first_blocks, pools, end, dead=DEAD)
    if not complete:
        out["gaps"].append("signature history truncated: the oldest signature seen may not be the creation")
    if truncated:
        out["gaps"].append(f"window had more than {max_txs} transactions; only the first {max_txs} were read")
    if missing:
        out["gaps"].append(f"{len(missing)} transactions could not be fetched")
    if not complete or truncated or missing:
        res["coverage"]["complete"] = False
        res["launch_dump"] = None
    res["times"] = {"creation": li.get("creation_time"), "first_sell": _slot_time(window, res["first_sell_block"]),
                    "end": _slot_time(window, window[-1]["slot"]) if window else None, "approx": True}
    out.update(res)
    return out


def mint_check(client, mint):
    out = {"stage": "mint", "chain_id": "solana", "token": mint, "gaps": []}
    meta = mint_info(client, mint)
    out["token_meta"] = meta or {}
    if not meta:
        out["summary"] = f"{mint} is not an SPL mint on Solana mainnet"
        return out
    holders = largest_holders(client, mint)
    if not holders:
        out["gaps"].append("holders unavailable: the public node rate-limits getTokenLargestAccounts; retry in a minute")
    kd = kinds(client, [h["owner"] for h in holders if h["owner"]])
    curve = bonding_curve(mint)
    for h in holders:
        h["kind"] = kd.get(h["owner"], "unknown")
        h["pct_supply"] = round(100 * h["amount"] / meta["total_supply"], 3) if meta["total_supply"] else None
        h["label"] = "pump.fun bonding curve" if h["owner"] == curve else ("Raydium v4 vault" if h["owner"] == RAYDIUM_V4_AUTHORITY else None)
    wallets = [h for h in holders if h["kind"] == "eoa"]
    out.update({"holders": holders, "bonding_curve": curve,
                "top10_wallet_pct": round(sum(h["pct_supply"] or 0 for h in wallets[:10]), 3) if meta["total_supply"] else None,
                "flags": [f for f, ok in (("mint authority still set", meta["mint_authority"]), ("freeze authority still set", meta["freeze_authority"]),
                                          ("Token-2022 mint (extensions possible)", meta["token_program"] == TOKEN22)) if ok]})
    return out


def triage(client, mint, window_s=1800, tracked=None, dev=None, top_n=5, max_txs=120):
    out = {"stage": "triage", "chain_id": "solana", "token": mint, "gaps": [], "timing": {}}
    t0 = time.monotonic()
    head = client.head()
    out["head"] = head
    if head["block"] is None:
        out["summary"] = "RPC unreachable; cannot triage."
        return out
    meta = mint_info(client, mint)
    out["token_meta"] = meta or {}
    if not meta:
        out["summary"] = f"{mint} is not an SPL mint on Solana mainnet"
        return out
    dec, supply = meta["decimals"], meta["total_supply"] or None
    since = head["block_time"] - window_s
    sigs, complete = signatures(client, mint, since_time=since, max_pages=30)
    window = [s for s in sigs if (s.get("blockTime") or 0) >= since and not s.get("err")]
    truncated = len(window) > max_txs
    window = window[:max_txs]  # newest first
    records, sol, txs, missing = records_for(client, mint, window)
    curve = bonding_curve(mint)
    market = curve if kinds(client, [curve]).get(curve) == "contract" else None
    pools, hits, kd = pools_for(client, records, market)
    tr = flows.trades(records, pools, DEAD)
    agg = flows.aggregate(tr)
    times = {s["slot"]: s.get("blockTime") for s in window}
    for t in tr:
        t["ts"] = times.get(t["block"])
    pct = (lambda v: round(100 * v / supply, 3)) if supply else (lambda v: None)
    fmt = lambda v: round(v / 10 ** dec, 2)
    out["coverage"] = {"from_block": min((s["slot"] for s in window), default=None), "to_block": head["block"],
                       "from_time": since, "to_time": head["block_time"], "transfers": len(records), "txs": len(window),
                       "window_s": window_s, "complete": complete and not truncated and not missing, "timestamps_approx": False,
                       "pools": sorted(pools), "pool_tx_hits": hits}
    if truncated:
        out["gaps"].append(f"more than {max_txs} transactions in the window; only the newest {max_txs} were read")
    if missing:
        out["gaps"].append(f"{len(missing)} transactions could not be fetched")
    out["gaps"].append("signatures come from the mint account: plain wallet-to-wallet transfers may be missing")
    if not pools:
        out["gaps"].append("no pool detected: every move classified as plain transfer")
    # launcher: cheap when the history is short, otherwise ask for --dev
    li = None
    if complete and len(sigs) <= 3000:
        li, _, _ = launch_info(client, mint, max_pages=4)
    elif not dev:
        out["gaps"].append("history too long to find the launcher cheaply; pass --dev")
    out["launch"] = li
    dev_set = {d for d in (dev or [])} | ({li["launcher"]} if li and li.get("launcher") else set())
    sellers = sorted(((a, d) for a, d in agg.items() if d["sold"]), key=lambda kv: -kv[1]["sold"])
    proceeds = defaultdict(int)
    for t in tr:
        if t["kind"] == "sell":
            proceeds[t["addr"]] += (sol.get(t["tx"]) or {}).get(t["addr"], 0)
    out["flow"] = {"sold": fmt(sum(d["sold"] for d in agg.values())), "bought": fmt(sum(d["bought"] for d in agg.values())),
                   "burned": fmt(sum(d["burned"] for d in agg.values())), "sellers": len(sellers),
                   "buyers": sum(1 for d in agg.values() if d["bought"])}
    out["top_sellers"] = [{"addr": a, "kind": kd.get(a, "unknown"), "sold": fmt(d["sold"]), "pct_supply": pct(d["sold"]), "trades": d["n"],
                           "sell_txs": d["txs"], "bought_in_window": fmt(d["bought"]), "sol_proceeds": round(proceeds[a] / LAMPORTS, 4),
                           "dev_linked": a in dev_set} for a, d in sellers[:top_n]]
    out["dev_selling"] = [{"addr": a, "sold": fmt(d["sold"]), "pct_supply": pct(d["sold"])} for a, d in sellers if a in dev_set]
    out["tracked"] = [{"addr": a, "sold": fmt(agg[a]["sold"]) if a in agg else 0, "bought": fmt(agg[a]["bought"]) if a in agg else 0}
                      for a in (tracked or [])]
    holders = largest_holders(client, mint)
    if not holders:
        out["gaps"].append("holders unavailable: the public node rate-limits getTokenLargestAccounts; retry in a minute")
    kd2 = kinds(client, [h["owner"] for h in holders if h["owner"]])
    for h in holders:
        h["kind"], h["pct_supply"] = kd2.get(h["owner"], "unknown"), pct(h["amount"])
    out["holders"] = holders
    out["top10_wallet_pct"] = round(sum(h["pct_supply"] or 0 for h in holders if h["kind"] == "eoa"), 3) if supply else None
    out["authorities"] = {"mint": meta["mint_authority"], "freeze": meta["freeze_authority"]}
    out["timing"]["total_s"] = round(time.monotonic() - t0, 2)
    return out
