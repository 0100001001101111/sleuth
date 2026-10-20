"""HTTP client for Blockscout API v2 + JSON-RPC: time budget, bounded concurrency, sqlite cache of durable facts."""
import json, sqlite3, threading, time
from concurrent.futures import ThreadPoolExecutor
from . import config as C

try:
    from curl_cffi import requests as _rq  # Cloudflare in front of Blockscout rejects plain curl/urllib TLS
except ImportError as e:  # pragma: no cover
    raise SystemExit("pip install curl_cffi  (needed to pass Blockscout's TLS fingerprint check)") from e


class BudgetExceeded(Exception):
    pass


class ApiError(Exception):
    pass


class Cache:
    """Durable facts only (immutable once confirmed). Never balances, holders or 'latest' lists."""

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("create table if not exists kv (k text primary key, v text, chain text, block integer, ts real)")
            c.execute("create table if not exists cursor (k text primary key, block integer, ts real)")

    def _conn(self):
        return sqlite3.connect(self.path, timeout=10)

    def get(self, key):
        with self._lock, self._conn() as c:
            r = c.execute("select v, block, ts from kv where k=?", (key,)).fetchone()
        return (json.loads(r[0]), {"block": r[1], "fetched_at": r[2], "cached": True}) if r else (None, None)

    def put(self, key, value, chain, block=None):
        with self._lock, self._conn() as c:
            c.execute("insert or replace into kv values (?,?,?,?,?)",
                      (key, json.dumps(value), str(chain), block, time.time()))

    def cursor(self, key):
        with self._lock, self._conn() as c:
            return c.execute("select block, ts from cursor where k=?", (key,)).fetchone()

    def set_cursor(self, key, block):
        with self._lock, self._conn() as c:
            c.execute("insert or replace into cursor values (?,?,?)", (key, block, time.time()))


class Client:
    def __init__(self, cfg=None, chain_id=None, budget_s=None, cache=True):
        self.cfg = cfg or C.load()
        self.chain_id, self.chain = C.chain(self.cfg, chain_id)
        self.base = self.chain["explorer"].rstrip("/") + "/api/v2"
        self.explorer = self.chain["explorer"].rstrip("/")
        self.rpc_url = self.chain["rpc"]
        self.h = self.cfg["http"]
        self.deadline = time.monotonic() + budget_s if budget_s else None
        self.requests = 0
        self.errors = []
        self._local = threading.local()
        self._pool = ThreadPoolExecutor(max_workers=self.h["concurrency"])
        self.cache = Cache(C.DATA / "cache.db") if cache else None
        self._lock = threading.Lock()
        self._rpc_lock = threading.Lock()
        # Token bucket over JSON-RPC *sub-calls* (the limiter counts batch items): measured burst ~150-200 then HTTP 429,
        # sustained ~15/s. Defaults are deliberately under that.
        self.rpc_burst = int(self.h.get("rpc_burst", 120))
        self.rpc_rate = float(self.h.get("rpc_rate", 14.0))
        self._tokens = float(self.rpc_burst)
        self._tokens_at = time.monotonic()

    # ---------- plumbing ----------
    def _sess(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._local.s = _rq.Session(impersonate="chrome")
        return s

    def remaining(self):
        return None if self.deadline is None else self.deadline - time.monotonic()

    def check_budget(self):
        r = self.remaining()
        if r is not None and r <= 0:
            raise BudgetExceeded()

    def _count(self):
        with self._lock:
            self.requests += 1

    def get(self, path, params=None, soft=False, timeout=None, retries=None):
        """GET an explorer API path. soft=True returns None on 404/5xx instead of raising. timeout caps one attempt."""
        self.check_budget()
        url = self.base + path
        last = None
        for attempt in range((self.h["retries"] if retries is None else retries) + 1):
            self._count()
            try:
                rem = self.remaining()
                t = timeout or self.h["timeout"]
                t = t if rem is None else min(t, max(2, rem))
                r = self._sess().get(url, params=params, timeout=t)
                if r.status_code == 404 and soft:
                    return None
                if r.status_code == 429:
                    last = ApiError("429 rate limited")
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code >= 500:
                    last = ApiError(f"{r.status_code} {r.text[:80]}")
                    time.sleep(0.5)
                    continue
                d = r.json()
                if not isinstance(d, (dict, list)):
                    last = ApiError(f"non-json body {str(d)[:60]}")
                    continue
                return d
            except (_rq.RequestsError, ValueError) as e:
                last = ApiError(str(e)[:120])
                time.sleep(0.3)
        if soft:
            self.errors.append(f"{path}: {last}")
            return None
        raise last or ApiError("unknown")

    def many(self, paths, timeout=None, retries=None):
        """Fetch independent paths concurrently (soft). Result list aligned with input."""
        futs = [self._pool.submit(self.get, p, None, True, timeout, retries) for p in paths]
        out = []
        for f in futs:
            try:
                out.append(f.result())
            except (BudgetExceeded, ApiError) as e:
                self.errors.append(f"many: {e}")
                out.append(None)
        return out

    def paginate(self, path, max_pages=5, stop=None):
        """Follow next_page_params. stop(item)->True ends early. Returns (items, complete)."""
        items, params = [], None
        for _ in range(max_pages):
            d = self.get(path, params, soft=True)
            if not d:
                return items, False
            for it in d.get("items") or []:
                if stop and stop(it):
                    return items, True
                items.append(it)
            params = d.get("next_page_params")
            if not params:
                return items, True
        return items, False

    # ---------- RPC ----------
    def rpc(self, calls, batch=40):
        """calls: [(method, params)]. Batched (<=40 per request; larger batches get 429'd), 429-aware.
        Returns results aligned with calls (None on error, error recorded)."""
        out = []
        for i in range(0, len(calls), batch):
            out.extend(self._rpc_once(calls[i:i + batch]))
        return out

    def _throttle(self, n):
        """Take n sub-call tokens (across threads), sleeping until the bucket has them."""
        n = min(n, self.rpc_burst)
        with self._rpc_lock:
            now = time.monotonic()
            self._tokens = min(self.rpc_burst, self._tokens + (now - self._tokens_at) * self.rpc_rate)
            self._tokens_at = now
            wait = 0.0 if self._tokens >= n else (n - self._tokens) / self.rpc_rate
            rem = self.remaining()
            if rem is not None and wait > rem:
                raise BudgetExceeded()  # checked before reserving, so an aborted call never delays other threads
            self._tokens -= n  # may go negative: later callers wait for it
        if wait > 0:
            time.sleep(wait)

    def _rpc_once(self, calls):
        self.check_budget()
        body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
        res = None
        for attempt in range(4):
            self._count()
            self._throttle(len(calls))
            try:
                rem = self.remaining()
                t = self.h["timeout"] if rem is None else min(self.h["timeout"], max(2, rem))
                r = self._sess().post(self.rpc_url, json=body, timeout=t)
                if r.status_code == 429:
                    self.errors.append("rpc: HTTP 429")
                    time.sleep(2.0 * (attempt + 1))
                    continue
                res = r.json()
            except (_rq.RequestsError, ValueError) as e:
                self.errors.append(f"rpc: {str(e)[:100]}")
                time.sleep(0.3)
                continue
            if isinstance(res, dict):
                res = [res]
            if any("Too Many Requests" in str((x.get("error") or {}).get("message", "")) for x in res if isinstance(x, dict)):
                self.errors.append("rpc: Too Many Requests")
                time.sleep(2.0 * (attempt + 1))
                continue
            break
        if res is None:
            return [None] * len(calls)
        if not isinstance(res, list):
            self.errors.append(f"rpc: unexpected envelope {str(res)[:60]}")
            return [None] * len(calls)
        by = {x.get("id"): x for x in res if isinstance(x, dict)}
        out = []
        for i, (m, _) in enumerate(calls):
            x = by.get(i) or {}
            if "error" in x:
                self.errors.append(f"rpc {m}: {x['error'].get('message')}"[:160])
            out.append(x.get("result"))
        return out

    def head(self):
        """Chain id + latest block. Raises ApiError if the RPC serves a different chain than configured (cache safety)."""
        cid, blk = self.rpc([("eth_chainId", []), ("eth_getBlockByNumber", ["latest", False])])
        blk = blk if isinstance(blk, dict) else None
        try:
            chain_id = int(cid, 16) if cid else None
            bn = int(blk["number"], 16) if blk else None
            bt = int(blk["timestamp"], 16) if blk else None
        except (TypeError, ValueError, KeyError) as e:
            raise ApiError(f"malformed head response: {e}")
        if chain_id is not None and str(chain_id) != str(self.chain_id):
            raise ApiError(f"RPC {self.rpc_url} serves chain {chain_id}, config says {self.chain_id}; refusing (cache would be poisoned)")
        return {"chain_id": chain_id, "block": bn, "block_time": bt, "time": time.time()}

    def total_supply(self, token, block="latest"):
        tag = block if isinstance(block, str) else hex(block)
        (r,) = self.rpc([("eth_call", [{"to": token, "data": "0x18160ddd"}, tag])])
        return int(r, 16) if r and r != "0x" else None

    # ---------- cached durable facts ----------
    def _cached(self, key, fetch, block=None):
        if self.cache:
            v, meta = self.cache.get(key)
            if v is not None:
                return v, meta
        v = fetch()
        if v is not None and self.cache:
            self.cache.put(key, v, self.chain_id, block)
        return v, {"cached": False, "fetched_at": time.time()}

    def address(self, addr, timeout=None, retries=None):
        """Address identity (contract?, name, creator, proxy). Durable."""
        def f():
            d = self.get(f"/addresses/{addr}", soft=True, timeout=timeout, retries=retries)
            if not d:
                return None
            keep = ("hash", "is_contract", "name", "is_verified", "creator_address_hash",
                    "creation_transaction_hash", "proxy_type", "public_tags", "is_scam")
            out = {k: d.get(k) for k in keep}
            out["implementations"] = [i.get("address") or i.get("address_hash") for i in d.get("implementations") or []]
            return out
        v, _ = self._cached(f"addr:{self.chain_id}:{addr.lower()}", f)
        return v or {"hash": addr, "is_contract": None, "name": None}

    def counters(self, addr, max_age_s=86400):
        """Tx counts; cached for a day (only used to spot high-volume service addresses, precision not needed)."""
        key = f"cnt:{self.chain_id}:{addr.lower()}"
        if self.cache:
            v, meta = self.cache.get(key)
            if v and time.time() - (meta or {}).get("fetched_at", 0) < max_age_s:
                return v
        d = self.get(f"/addresses/{addr}/counters", soft=True) or {}
        try:
            v = {"tx": int(d.get("transactions_count") or 0), "transfers": int(d.get("token_transfers_count") or 0)}
        except (TypeError, ValueError):
            return {"tx": 0, "transfers": 0}
        if self.cache and d:
            self.cache.put(key, v, self.chain_id)
        return v

    def token(self, token):
        """Token metadata. Identity cached; supply/holders always refreshed."""
        def f():
            d = self.get(f"/tokens/{token}", soft=True)
            if not d:
                return None
            return {k: d.get(k) for k in ("address", "address_hash", "name", "symbol", "decimals", "type")}
        v, _ = self._cached(f"token:{self.chain_id}:{token.lower()}", f)
        if not v:
            return None
        live = self.get(f"/tokens/{token}", soft=True) or {}
        v = dict(v)
        v["total_supply"] = live.get("total_supply")
        v["holders"] = live.get("holders_count") or live.get("holders")
        v["decimals"] = int(v.get("decimals") or 18)
        return v

    def tx(self, h):
        """Full transaction incl. token_transfers. Cached (immutable once confirmed)."""
        def f():
            d = self.get(f"/transactions/{h}", soft=True)
            if not d or d.get("result") == "pending":
                return None
            return d
        v, _ = self._cached(f"tx:{self.chain_id}:{h.lower()}", f)
        return v

    def tx_internal(self, h):
        def f():
            d = self.get(f"/transactions/{h}/internal-transactions", soft=True)
            return (d or {}).get("items") if d else None
        v, _ = self._cached(f"txint:{self.chain_id}:{h.lower()}", f)
        return v or []

    def link(self, kind, value):
        return f"{self.explorer}/{kind}/{value}"

    def close(self):
        self._pool.shutdown(wait=False)
