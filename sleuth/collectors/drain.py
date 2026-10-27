"""Drained-wallet INVESTIGATION (versioned, read-only, explorer-fed, multi-chain).

The event screen in `wallet.py` lists logs in a window on one chain and stops. This collector does what an investigator
does next, with the explorer (Blockscout API v2) as the only source:

  1. presence: where the wallet has activity at all (every configured chain), so a drain on another chain is not
     reported as "no transfers";
  2. inventory: every asset outflow from the wallet in the pages read, with address-poisoning rows quarantined (kept,
     flagged, never summed) rather than deleted;
  3. mechanism: each loss transaction is fetched and the ROLES separated (fee payer / submitter, EIP-7702 authority
     and delegate, called contract, asset recipient) and classified by what the chain proves:
       eip7702_sweep              an authorization signed by this account, submitted by another party, assets left
                                  in that same transaction
       allowance_used_by_spender  transferFrom(victim, ...) submitted by someone else
       permit2_signature_used / permit_signature_used   a signature-based allowance path (lead, not proof)
       call_into_delegated_account   a third party called the account itself (delegate code acted)
       wallet_key_signed_transfer / wallet_key_signed_contract_call / wallet_signed_delegation_tx
                                  a valid signature from this wallet's key submitted the transaction
       third_party_tx_unresolved  assets left in a transaction someone else sent and the path is not decodable
                                  without a trace
     "private key compromised" is never a verdict: a valid signature is consistent with theft, malware, a deceptive
     UI or an intentional action; the page says exactly which signatures exist and who submitted them;
  4. victim profile: protocols and contracts this wallet used (its own deployments are marked), approvals it signed,
     first funding; these are LEADS for "how", never named as the cause;
  5. suspects: recipients and submitters of the loss transactions, each with its ROLE kept: "other accounts that
     authorized this submitter" is asked only of submitters, "still holds / sent on" only of recipients and only for
     this wallet's assets after the first loss; delegate contracts and their code hash (same kit?), gas funder, and
     where their outflows went, with explorer tags classified (exchange / privacy pool / bridge / DEX) as leads;
  6. three-valued verdicts, "no" only when every configured chain answered and every active chain was read in full;
     a chain skipped for time, budget or a provider failure turns every negative into unknown.

Budget: explorer reads fan out in threads; the first (1 - SUSPECT_SHARE) of the budget is for the victim side, the rest
for suspects. The first batch of loss transactions is always fetched even past that soft line (one batch is the
minimum useful read), and the case's own chain is read first even when another chain is busier: both by design.

Identity of a token is (chain, contract address), never a symbol. Amounts stay raw strings with decimals beside them.
Ownership of any address is unknown unless the chain shows it (a creator relation, a self-funding loop).
"""
import hashlib
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

from ..client import ApiError, BudgetExceeded, Client
from .money import _h, _int, _ts
from .types import COLLECTOR_VERSION

RULE_VERSION = "drain-investigation/0.1"
NATIVE = "native"
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
_PROVIDER_ERRORS = (ApiError, OSError, ValueError, TypeError, KeyError, AttributeError)
IMPERSONATED = frozenset({"USDC", "USDT", "DAI", "ETH", "WETH", "USDP", "PYUSD", "USDG", "WBTC", "USDE", "BUSD"})
_PRIVACY = ("railgun", "tornado", "mixer", "privacy")
_EXCHANGE = ("binance", "coinbase", "kraken", "okx", "bybit", "kucoin", "gate.io", "htx", "huobi", "bitget", "mexc", "crypto.com",
             "bitfinex", "gemini", "exchange", "hot wallet", "robinhood")
_BRIDGE = ("bridge", "relay", "across", "stargate", "hop protocol", "wormhole", "layerzero", "cctp", "solver")
_SERVICE = ("bundler", "entry point", "paymaster")   # ERC-4337 relays: a service many wallets pay, never a party to a loss
HOP_CAP = 3                    # next-hop addresses profiled per run: the addresses a loss recipient pays afterwards
HOP_PAGES = 14                 # transaction pages walked per next hop to reach its FIRST funding (about 700 txs)
_CONSTANT_ADDR = re.compile(r"constant\s+(\w+)\s*=\s*(0x[0-9a-fA-F]{40})")
_DEX = ("uniswap", "sushiswap", "curve", "balancer", "1inch", "pool manager", "router", "psm", "aggregator", "cow", "dex", "liquidity pool")
THIRD_PARTY = frozenset({"eip7702_sweep", "allowance_used_by_spender", "permit2_signature_used", "permit_signature_used",
                         "call_into_delegated_account", "third_party_tx_unresolved"})
SELF_SIGNED = frozenset({"wallet_key_signed_transfer", "wallet_key_signed_contract_call", "wallet_signed_delegation_tx"})
# delegation-based mechanisms: the only ones whose timing ties a nearby key-signed outflow to the loss. A permit or
# allowance spent by a relayer is often a swap the owner ordered, so proximity to it proves nothing.
SWEEP_LIKE = frozenset({"eip7702_sweep", "call_into_delegated_account"})
# how strongly a loss transaction nominates its parties for profiling
_WEIGHT = {"eip7702_sweep": 10, "call_into_delegated_account": 10, "allowance_used_by_spender": 5, "permit2_signature_used": 5,
           "permit_signature_used": 5, "third_party_tx_unresolved": 1}
CREDIT_WINDOW_S = 120          # a sweep this soon after a credit is a "watched address" lead
LOSS_TX_CAP = 40               # full-tx fetches per chain (2-3 s each)
SUSPECT_CAP = 4
SUSPECT_SHARE = 0.5            # share of the budget kept back for suspect profiling (the "where did it go" half): two pairs of ~45 s each on a slow explorer
SUSPECT_PAIR = 2               # suspects profiled side by side
T4_CAP = 8                    # type-4 txs fetched per suspect (other authorities)
PAGES_PRIMARY, PAGES_SECONDARY, PAGES_SUSPECT = 4, 2, 2


def _label(o):
    """Explorer address object -> (text label, kind). kind from explorer tags only: a LEAD, stated as such."""
    o = o if isinstance(o, dict) else {}
    parts = [o.get("name") or ""]
    for t in o.get("public_tags") or []:
        parts.append(t if isinstance(t, str) else str((t or {}).get("name") or (t or {}).get("label") or ""))
    for t in ((o.get("metadata") or {}).get("tags") or []):
        if isinstance(t, dict):
            parts.append(str(t.get("name") or ""))
    for i in o.get("implementations") or []:
        if isinstance(i, dict) and i.get("name"):
            parts.append(str(i["name"]))
    text = " | ".join(p for p in parts if p)
    low = text.lower()
    kind = "unknown"
    if any(w in low for w in _PRIVACY):
        kind = "privacy_pool"
    elif any(w in low for w in _SERVICE):
        kind = "bundler"
    elif any(w in low for w in _EXCHANGE) or "hot wallet" in low or "deposit address" in low:
        kind = "exchange"
    elif any(w in low for w in _BRIDGE):
        kind = "bridge"
    elif any(w in low for w in _DEX):
        kind = "dex"
    elif o.get("is_contract"):
        kind = "contract"
    elif o.get("is_contract") is False:
        kind = "eoa"
    if o.get("is_scam"):
        text = (text + " | " if text else "") + "flagged scam by explorer"
    return text, kind


def _tok(x):
    t = x.get("token") if isinstance(x, dict) else None
    return t if isinstance(t, dict) else {}


def _tok_addr(t):
    return (t.get("address_hash") or t.get("address") or "").lower() if isinstance(t, dict) else ""


def _total(x):
    tot = x.get("total") if isinstance(x, dict) else None
    tot = tot if isinstance(tot, dict) else {}
    raw = _int(tot.get("value"))
    dec = _int(tot.get("decimals"))
    if dec is None:
        dec = _int(_tok(x).get("decimals"))
    return raw, dec


def _sym_ascii(sym):
    """Symbol with invisible / homoglyph characters exposed: (normalised upper-case ASCII, had_non_ascii)."""
    s = str(sym or "")
    norm = unicodedata.normalize("NFKC", s)
    ascii_only = "".join(c for c in norm if ord(c) < 128 and c.isprintable())
    return ascii_only.upper().strip(), ascii_only != norm or any(ord(c) > 127 for c in s)


def _lookalike(a, b):
    """Two different addresses sharing the first 4 and last 4 hex characters (what wallets display)."""
    return a != b and len(a) == 42 and len(b) == 42 and a[:6] == b[:6] and a[-4:] == b[-4:]


def _hardcoded_addresses(src, creator=None):
    """Addresses baked into a VERIFIED delegate's source as constants (a sweeper's RECEIVER). What the code DOES with them is not
    established here (an ENTRY_POINT or token constant is not a receiver); they are reported as constants. A sweeper
    moves. Read from the explorer's source text only; an unverified delegate yields []. The live re-delegation of a drained
    wallet to a verified "EIP7702Delegate" whose RECEIVER was its own deployer is how this was found by hand (2026-10-27)."""
    if not isinstance(src, str) or not src:
        return []
    seen, out = set(), []
    for name, addr in _CONSTANT_ADDR.findall(src):
        a = addr.lower()
        if a in seen or a == "0x" + "0" * 40:
            continue
        seen.add(a)
        out.append({"name": name, "addr": a, "is_creator": bool(creator) and a == creator})
    return out[:4]


def _code_hash(client, addr, prefetched=None):
    """Deployed bytecode of a contract via the explorer (works for unverified code). (sha256 hex, length) or (None, None)."""
    d = prefetched if isinstance(prefetched, dict) else (client.get(f"/smart-contracts/{addr}", soft=True) or {})
    bc = d.get("deployed_bytecode") or d.get("creation_bytecode") or ""
    if not isinstance(bc, str) or len(bc) < 4:
        return None, None
    return hashlib.sha256(bc.lower().encode()).hexdigest(), (len(bc) - 2) // 2


class _Investigation:
    def __init__(self, client, victim, incident_ts, chains):
        self.client, self.victim, self.incident_ts = client, victim.lower(), incident_ts
        self.cfg = getattr(client, "cfg", None) or {}
        self.chains = chains
        self.gaps = []
        self.complete = True
        self.exhausted = False
        self._info = {}                    # chain_id -> explorer address record of the victim (presence() fills it)
        self.chains_unread = set()         # active chains skipped (cap, soft deadline, budget, provider): negatives become unknown
        self.suspects_unprofiled = False   # a candidate was capped or ran out of budget: suspect negatives become unknown
        self.hops_unprofiled = False       # a next hop was not profiled (budget): hop negatives become unknown
        self.t0 = time.time()
        self.soft_deadline = None          # monotonic time after which victim-side reads stop so the suspects get profiled

    def past_soft(self):
        return self.soft_deadline is not None and time.monotonic() > self.soft_deadline

    def fanout(self, jobs):
        """Run independent explorer reads side by side, one thread each (the explorer answers in 2-5 s per call, so
        sequential reads were the whole budget). Results in input order; every job finishes before the first exception
        is re-raised, so a BudgetExceeded in one read never loses the pages another read already got."""
        if len(jobs) == 1:
            return [jobs[0]()]
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futs = [pool.submit(j) for j in jobs]
        res, err = [], None
        for f in futs:
            try:
                res.append(f.result())
            except Exception as e:          # noqa: BLE001 - re-raised below after the others are collected
                res.append(None)
                err = err or e
        if err is not None:
            raise err
        return res

    def gap(self, kind, detail, chain=None):
        self.gaps.append({"kind": kind, "detail": detail[:240], **({"chain_id": chain} if chain is not None else {})})
        if kind in ("truncated", "budget", "provider", "capped"):
            self.complete = False

    # -- explorer plumbing -----------------------------------------------------------------------------------------
    def pages(self, cl, addr, what, n):
        """Address pages; a budget expiry here is recorded and returned as an incomplete read, never raised, so the
        sibling reads running beside it (fanout) keep what they got."""
        n0 = len(cl.errors)
        try:
            raw, ok = cl.paginate(f"/addresses/{addr}/{what}", max_pages=n)
        except BudgetExceeded:
            self.exhausted = True
            self.gap("budget", f"{what} for {addr[:12]}: budget ran out before the pages were read", cl.chain_id)
            return [], False
        items = [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []
        if not isinstance(raw, list):
            self.gap("provider", f"{what} for {addr[:12]}: unexpected response shape", cl.chain_id)
            return [], False
        if len(items) != len(raw):            # a malformed row could have been the one that mattered: the page is not complete
            self.gap("provider", f"{what} for {addr[:12]}: {len(raw) - len(items)} malformed item(s) skipped", cl.chain_id)
            ok = False
        if not ok and len(cl.errors) > n0:
            self.gap("provider", f"{what} for {addr[:12]}: a page read failed ({len(items)} item(s) kept)", cl.chain_id)
        elif not ok:
            self.gap("truncated", f"{what} for {addr[:12]}: only the first {n} page(s) read (oldest {items[-1].get('timestamp') if items else '?'})", cl.chain_id)
        return items, ok

    def fetch_txs(self, cl, hashes, until=None):
        """Full transactions, several at a time (the explorer takes 2-3 s per fetch; sequential fetches ate the whole
        budget on a busy mainnet wallet). Returns ({hash: tx_or_None}, budget_ran_out, deferred). A hash maps to None when
        the explorer failed on it; hashes after a budget expiry are absent, never None, so the caller can tell the two
        apart. `until` (monotonic) is a soft stop: after the first batch, no new batch starts past it (deferred=True)."""
        got, hashes = {}, [h for h in hashes if h]
        width = max(1, int((getattr(cl, "h", None) or {}).get("concurrency") or 4)) * 2
        for i in range(0, len(hashes), width):
            batch = hashes[i:i + width]
            if i > 0 and until is not None and time.monotonic() > until:
                return got, False, True
            try:
                cl.check_budget()
            except BudgetExceeded:
                self.exhausted = True
                return got, True, False
            n_err = len(cl.errors)
            res = cl.many([f"/transactions/{h}" for h in batch])
            for h, d in zip(batch, res):
                got[h] = d if isinstance(d, dict) and d.get("result") != "pending" else None
            if (cl.remaining() is not None and cl.remaining() <= 0) or any("BudgetExceeded" in e or "budget" in e.lower() for e in cl.errors[n_err:]):
                self.exhausted = True
                for h in batch:          # a batch cut by the deadline: unfetched, not failed
                    if got.get(h) is None:
                        got.pop(h, None)
                return got, True, False
        return got, False, False

    def canonical(self, chain_id):
        ch = (self.cfg.get("chains") or {}).get(str(chain_id)) or {}
        return {str(k).upper(): str(v).lower() for k, v in (ch.get("canonical_tokens") or {}).items()}

    # -- 1. presence -----------------------------------------------------------------------------------------------
    def presence(self):
        """Per chain: does the explorer know this address at all, and how busy is it. Counters alone are not trusted:
        Blockscout returns a stale "0" for some live addresses, so an address record that exists with a balance or token
        history counts as present. A 404 on the address record is the only clean negative."""
        out = {}

        def one(cid, cl):
            try:
                info = cl.get(f"/addresses/{self.victim}", soft=True)
                d = cl.get(f"/addresses/{self.victim}/counters", soft=True) if info is not None else {}
            except BudgetExceeded:
                self.exhausted = True
                self.gap("budget", f"presence check stopped before chain {cid}", cid)
                return None
            except _PROVIDER_ERRORS as e:
                self.gap("provider", f"address or counters failed on chain {cid}: {type(e).__name__}", cid)
                return (None, None)
            return (info, d)
        chains = list(self.chains.items())
        results = self.fanout([lambda cid=cid, cl=cl: one(cid, cl) for cid, cl in chains])   # the chains answer in parallel
        for (cid, cl), r in zip(chains, results):
            if r is None:
                continue                                    # budget ran out: presence on this chain stays unknown
            info, d = r
            if info is None and not cl.errors:
                out[cid] = {"tx": 0, "transfers": 0, "name": cl.chain.get("name"), "seen": False}   # explorer has never seen it
                continue
            if not isinstance(d, dict) or not isinstance(info, dict):
                out[cid] = {"tx": None, "transfers": None, "name": cl.chain.get("name"), "seen": None}
                self.gap("provider", f"presence unknown on chain {cid} (address or counters unavailable)", cid)
                continue
            tx, tr = _int(d.get("transactions_count")), _int(d.get("token_transfers_count"))
            stale = not (tx or tr) and (bool(_int(info.get("coin_balance"))) or info.get("has_token_transfers") or info.get("has_tokens"))
            out[cid] = {"tx": tx if not stale else max(tx or 0, 1), "transfers": tr, "name": cl.chain.get("name"), "seen": True,
                        "counters_stale": bool(stale)}
            self._info[cid] = info
        return out

    # -- 1b. current delegation (EIP-7702) -------------------------------------------------------------------------
    def delegation(self, cl):
        """Is the wallet delegated to code RIGHT NOW. A delegation is not a token approval: revoking approvals leaves it
        in place, and the delegate code can move anything the key could. Reported with the delegate's identity so the
        reader can tell a wallet-vendor account (verified, named) from an unverified sweeper."""
        info = self._info.get(cl.chain_id)
        if info is None:
            try:
                info = cl.get(f"/addresses/{self.victim}", soft=True)
            except _PROVIDER_ERRORS as e:
                info = None
                self.gap("provider", f"delegation check failed on chain {cl.chain_id}: {type(e).__name__}", cl.chain_id)
        if not isinstance(info, dict):
            return {"known": False, "delegated": None, "delegate": None}
        impls = [(i.get("address_hash") or "").lower() for i in info.get("implementations") or [] if isinstance(i, dict)]
        delegated = info.get("proxy_type") == "eip7702" and bool(impls)
        out = {"known": True, "delegated": delegated, "delegate": impls[0] if delegated else None, "label": None, "verified": None,
               "is_scam": None, "code_len": None, "code_sha256": None}
        if delegated:
            try:
                d, sc = cl.many([f"/addresses/{impls[0]}", f"/smart-contracts/{impls[0]}"])
                d, sc = (d if isinstance(d, dict) else {}), (sc if isinstance(sc, dict) else {})
                out["label"] = _label(d)[0] or None
                out["verified"] = d.get("is_verified")
                out["is_scam"] = bool(d.get("is_scam"))
                out["code_sha256"], out["code_len"] = _code_hash(cl, impls[0], sc)
                out["creator"] = (d.get("creator_address_hash") or "").lower() or None
                out["forwards_to"] = _hardcoded_addresses(sc.get("source_code"), out["creator"]) if sc.get("is_verified") else []   # address constants in verified source; what they are used for is not established here   # a verified sweeper names its receiver
            except _PROVIDER_ERRORS as e:
                self.gap("provider", f"delegate {impls[0][:12]} not readable on chain {cl.chain_id}: {type(e).__name__}", cl.chain_id)
        return out

    # -- 2. inventory + quarantine ---------------------------------------------------------------------------------
    def inventory(self, cl, n_pages):
        v, cid = self.victim, cl.chain_id
        canon = self.canonical(cid)
        canon_addrs = set(canon.values())
        native_sym = str(cl.chain.get("symbol", "ETH")).upper()
        (tts, ok1), (txs, ok2), (its, ok3) = self.fanout([
            lambda: self.pages(cl, v, "token-transfers", n_pages),
            lambda: self.pages(cl, v, "transactions", n_pages),
            lambda: self.pages(cl, v, "internal-transactions", min(2, n_pages))])
        # real counterparties = addresses the wallet SENT value to in a tx it signed, or received a canonical asset from
        real = set()
        for t in txs:
            if _h(t.get("from")) == v and _h(t.get("to")):
                real.add(_h(t.get("to")))
        for x in tts:
            if _tok_addr(_tok(x)) in canon_addrs:
                real.add(_h(x.get("from")) if _h(x.get("to")) == v else _h(x.get("to")))
        real.discard(v)
        outflows, credits, quarantine = [], [], []
        for x in tts:
            tok = _tok(x)
            ta = _tok_addr(tok)
            raw, dec = _total(x)
            frm, to = _h(x.get("from")), _h(x.get("to"))
            sym_ascii, odd = _sym_ascii(tok.get("symbol"))
            reason = None
            if ta not in canon_addrs:
                if odd:
                    reason = "token symbol uses look-alike characters"
                elif sym_ascii == native_sym:
                    reason = f"token calls itself {native_sym}, the native coin, which is not a token contract"
                elif sym_ascii in IMPERSONATED and sym_ascii in canon:
                    reason = f"token calls itself {sym_ascii} but is not the {sym_ascii} contract on this chain"
                elif tok.get("is_scam") or (to == v and (x.get("from") or {}).get("is_scam")):
                    reason = "explorer flags the token or the sending address as scam"     # outgoing rows: the submitter rule in mechanisms()
                elif raw == 0:
                    reason = "zero-value transfer"
            cp = frm if to == v else to
            lookalike = bool(cp) and any(_lookalike(cp, r) for r in real)
            # a look-alike counterparty is poisoning bait when it SENDS (or sends a fake token); a real asset the wallet
            # SENT to a look-alike is the poisoning payoff, the loss itself, and is kept as an outflow, flagged
            if reason is None and lookalike and not (frm == v and ta in canon_addrs):
                reason = "counterparty address looks like one this wallet really dealt with (same first and last characters)"
            row = {"tx": (x.get("transaction_hash") or "").lower(), "ts": x.get("timestamp"), "token": ta, "symbol": tok.get("symbol"),
                   "standard": tok.get("type"), "raw": str(raw) if raw is not None else None, "decimals": dec,
                   "from": frm, "to": to, "canonical": ta in canon_addrs, "to_label": _label(x.get("to"))[0], "from_label": _label(x.get("from"))[0],
                   "lookalike_recipient": bool(lookalike and frm == v and ta in canon_addrs)}
            if reason:
                quarantine.append({**row, "reason": reason})
            elif frm == v:
                outflows.append({**row, "asset": ta, "via": "token"})
            elif to == v:
                credits.append(row)
        for t in txs:
            val = _int(t.get("value"))
            if not val:
                continue
            row = {"tx": (t.get("hash") or "").lower(), "ts": t.get("timestamp"), "asset": NATIVE, "symbol": cl.chain.get("symbol", "ETH"), "raw": str(val),
                   "decimals": 18, "from": _h(t.get("from")), "to": _h(t.get("to")), "canonical": True, "to_label": _label(t.get("to"))[0], "via": "tx"}
            if row["from"] == v and (t.get("status") in (None, "ok")):
                row["lookalike_recipient"] = bool(row["to"]) and any(_lookalike(row["to"], r) for r in real)
                outflows.append(row)
            elif row["to"] == v:
                credits.append(row)
        for i in its:
            val = _int(i.get("value"))
            if not val or i.get("success") is False:
                continue
            row = {"tx": (i.get("transaction_hash") or "").lower(), "ts": i.get("timestamp"), "asset": NATIVE, "symbol": cl.chain.get("symbol", "ETH"),
                   "raw": str(val), "decimals": 18, "from": _h(i.get("from")), "to": _h(i.get("to")), "canonical": True, "to_label": _label(i.get("to"))[0], "via": "internal"}
            if row["from"] == v:
                row["lookalike_recipient"] = bool(row["to"]) and any(_lookalike(row["to"], r) for r in real)
                outflows.append(row)
            elif row["to"] == v:
                credits.append(row)
        return {"chain_id": cid, "outflows": outflows, "credits": credits, "quarantine": quarantine, "txs": txs,
                "pages_complete": ok1 and ok2 and ok3, "oldest_read": min((x.get("timestamp") or "~" for x in tts + txs), default=None),
                "canonical": canon}

    # -- 3. mechanism ----------------------------------------------------------------------------------------------
    @staticmethod
    def classify(full, account, assets_in_tx):
        """One fetched transaction -> mechanism record for `account` (the address whose assets left in it)."""
        submitter = _h(full.get("from"))
        to = _h(full.get("to"))
        auths = [a for a in (full.get("authorization_list") or []) if isinstance(a, dict) and (a.get("authority") or "").lower() == account]
        dec = full.get("decoded_input") if isinstance(full.get("decoded_input"), dict) else {}
        method = (full.get("method") or dec.get("method_call") or "")
        mlow = str(method).lower()
        raw_in = full.get("raw_input") or ""
        rec = {"tx": (full.get("hash") or "").lower(), "ts": full.get("timestamp"), "block": _int(full.get("block_number")), "submitter": submitter,
               "to": to, "to_label": _label(full.get("to"))[0], "type": str(full.get("type")) if full.get("type") is not None else None,
               "method": str(method)[:60] or None, "authorizations": [{"delegate": (a.get("address_hash") or "").lower(), "nonce": _int(a.get("nonce"))} for a in auths],
               "status": full.get("status")}
        if auths and submitter != account:
            rec["mechanism"] = "eip7702_sweep"
            a0 = rec["authorizations"][0]
            rec["evidence"] = (f"authorization signed by this account (nonce {a0['nonce']}) delegating to {a0['delegate'][:12]}; "
                               f"submitted and paid for by {submitter[:12]}; assets left in the same transaction")
        elif submitter == account:
            if auths:
                rec["mechanism"] = "wallet_signed_delegation_tx"
            elif (to in assets_in_tx and mlow.startswith("transfer")) or (raw_in in ("0x", "") and _int(full.get("value"))):
                rec["mechanism"] = "wallet_key_signed_transfer"   # token transfer call, or a plain native send
            else:
                rec["mechanism"] = "wallet_key_signed_contract_call"
            called = f" calling {to[:12]} {method}" if rec["mechanism"].endswith("call") else ""
            rec["evidence"] = f"transaction signed with this wallet's key (nonce {_int(full.get('nonce'))}){called}"
        elif to == PERMIT2:
            rec["mechanism"] = "permit2_signature_used"
            rec["evidence"] = f"submitted by {submitter[:12]} through the Permit2 contract; an off-chain signature from this wallet is the usual path (not decodable here)"
        elif mlow.startswith(("transferfrom", "safetransferfrom")) and to in assets_in_tx:
            rec["mechanism"] = "allowance_used_by_spender"
            rec["evidence"] = f"transferFrom submitted by {submitter[:12]}: a spender used an allowance this wallet had granted (or a permit)"
        elif "permit" in mlow:
            rec["mechanism"] = "permit_signature_used"
            rec["evidence"] = f"submitted by {submitter[:12]} with a permit-shaped call; a signature from this wallet is implied, not proven here"
        elif to == account:
            rec["mechanism"] = "call_into_delegated_account"
            rec["evidence"] = f"{submitter[:12]} called this account directly: code delegated by the account executed (delegation at that block not recorded)"
        else:
            rec["mechanism"] = "third_party_tx_unresolved"
            rec["evidence"] = f"assets left in a transaction sent by {submitter[:12]} to {to[:12]}; the path needs a trace this collector cannot read"
        return rec

    def mechanisms(self, cl, inv):
        """Fetch each loss transaction (cap) and classify. Returns (records, all_fetched)."""
        v = self.victim
        by_tx = {}
        for o in inv["outflows"]:
            by_tx.setdefault(o["tx"], []).append(o)
        # priority: canonical-asset rows first (poisoning bots time their fake transfers to land right beside the real
        # sweep, so "nearest the incident" alone fetched a dozen fakes and deferred the sweep), then near the incident
        # time, then recency
        def prio(h):
            rows = by_tx[h]
            ts = _ts(rows[0].get("ts")) or 0
            near = abs(ts - self.incident_ts) if self.incident_ts else 0
            return (0 if any(r["canonical"] for r in rows) else 1, near, -ts)
        order = sorted(by_tx, key=prio)
        if len(order) > LOSS_TX_CAP:
            self.gap("capped", f"{len(order)} outflow transactions; the first {LOSS_TX_CAP} by relevance were fetched", cl.chain_id)
        records, fetched_all = [], len(order) <= LOSS_TX_CAP
        wanted = [h for h in order[:LOSS_TX_CAP] if h]
        fulls, ran_out, deferred = self.fetch_txs(cl, wanted, until=self.soft_deadline)
        if ran_out:
            fetched_all = False
            self.gap("budget", f"budget exhausted while fetching loss transactions on chain {cl.chain_id}: {len(fulls)} of {len(wanted)} fetched", cl.chain_id)
        elif deferred:
            fetched_all = False
            self.gap("capped", f"loss transactions on chain {cl.chain_id}: {len(fulls)} of {len(wanted)} fetched; the rest were skipped to keep time for profiling the suspects", cl.chain_id)
        dropped = set()
        for h in wanted:
            if h not in fulls:
                continue                       # cut by the deadline: listed in the gap above, no record
            full = fulls[h]
            if not isinstance(full, dict):
                fetched_all = False
                self.gap("provider", f"tx {h[:12]} could not be fetched", cl.chain_id)
                records.append({"tx": h, "ts": by_tx[h][0].get("ts"), "mechanism": None, "evidence": "transaction could not be fetched", "outflows": by_tx[h]})
                continue
            assets = {r["asset"] for r in by_tx[h] if r["asset"] != NATIVE}
            if (full.get("from") or {}).get("is_scam") and not any(r.get("canonical") for r in by_tx[h]):
                # a poisoning bot moving its own fake token "from" the wallet: set the rows aside, do not count a loss
                for r in by_tx[h]:
                    inv["quarantine"].append({**r, "reason": "explorer flags the submitting address as scam and the asset is not a canonical token"})
                dropped.add(h)
                continue
            rec = self.classify(full, v, assets)
            rec["outflows"] = by_tx[h]
            rec["recipients"] = sorted({r["to"] for r in by_tx[h] if r.get("to")})
            # same-block / seconds-after-credit timing (watched address lead)
            ts = _ts(rec.get("ts"))
            if ts is not None and (rec["mechanism"] in SWEEP_LIKE or rec["mechanism"] == "third_party_tx_unresolved"):
                before = [c for c in inv["credits"] if (_ts(c.get("ts")) or 1e18) <= ts and c.get("canonical")]
                if before:
                    last = max(before, key=lambda c: _ts(c.get("ts")) or 0)
                    lag = ts - (_ts(last.get("ts")) or ts)
                    if 0 <= lag <= CREDIT_WINDOW_S:
                        rec["after_credit_s"] = lag
                        rec["credit_tx"] = last.get("tx")
            records.append(rec)
        if dropped:
            inv["outflows"] = [o for o in inv["outflows"] if o["tx"] not in dropped]     # quarantined rows are never counted
        return records, fetched_all

    # -- 4. victim profile -----------------------------------------------------------------------------------------
    def profile(self, cl, inv):
        v = self.victim
        used, approvals, own, funders = {}, [], [], []
        for t in inv["txs"]:
            frm, to = _h(t.get("from")), t.get("to") if isinstance(t.get("to"), dict) else {}
            toh = _h(to)
            if to and frm == v and toh:
                u = used.setdefault(toh, {"addr": toh, "label": _label(to)[0], "kind": _label(to)[1], "n": 0, "first": None, "last": None, "methods": {},
                                          "own_contract": (to.get("creator_address_hash") or "").lower() == v, "scam_flag": bool(to.get("is_scam"))})
                u["n"] += 1
                ts = t.get("timestamp")
                if isinstance(ts, str):
                    u["first"] = min(u["first"] or ts, ts)
                    u["last"] = max(u["last"] or ts, ts)
                dec = t.get("decoded_input") if isinstance(t.get("decoded_input"), dict) else {}
                m = str(t.get("method") or dec.get("method_call") or ("native transfer" if _int(t.get("value")) else "?"))[:40]
                u["methods"][m] = u["methods"].get(m, 0) + 1
                if any(k in m.lower() for k in ("approv", "permit", "increaseallowance")):
                    ps = [p for p in (dec.get("parameters") or []) if isinstance(p, dict)]
                    spender = next((str(p.get("value")) for p in ps if "spender" in str(p.get("name", "")).lower() or "operator" in str(p.get("name", "")).lower()), None)
                    val = next((str(p.get("value")) for p in ps if "value" in str(p.get("name", "")).lower() or "amount" in str(p.get("name", "")).lower() or "approved" in str(p.get("name", "")).lower()), None)
                    approvals.append({"ts": ts, "tx": (t.get("hash") or "").lower(), "token": toh, "token_label": _label(to)[0], "method": m,
                                      "spender": (spender or "").lower() or None, "value": val})
            elif isinstance(t.get("created_contract"), dict) and frm == v and _h(t.get("created_contract")):
                own.append(_h(t.get("created_contract")))
            if toh == v and _int(t.get("value")) and frm:
                funders.append({"ts": t.get("timestamp"), "from": frm, "label": _label(t.get("from"))[0], "raw": str(_int(t.get("value")))})
        own = sorted(set(own) | {a for a, u in used.items() if u["own_contract"]})
        funders.sort(key=lambda f: f.get("ts") or "~")
        top = sorted(used.values(), key=lambda u: -u["n"])
        return {"protocols": top[:25], "protocols_total": len(top), "approvals": approvals[:40], "own_contracts": own[:20],
                "first_funding": funders[0] if funders else None, "funders": len({f["from"] for f in funders}),
                "history_complete": inv["pages_complete"], "oldest_read": inv["oldest_read"]}

    # -- 5. suspects -----------------------------------------------------------------------------------------------
    def suspect(self, cl, addr, swept_assets, since_ts=None, roles=()):
        """Profile one party of the loss transactions. `roles` is what the chain showed it doing (submitter / recipient);
        `swept_assets` are THIS victim's assets that reached a loss recipient; `since_ts` is the first loss on this chain,
        so outbound rows carry whether they happened after it (proceeds) or before (unrelated history)."""
        a = addr.lower()
        s = {"addr": a, "chain_id": cl.chain_id, "roles": sorted(set(roles)), "label": None, "kind": None, "is_contract": None, "tx_count": None,
             "other_authorities": None, "authorities_sample": [], "delegates": [], "swept_in": {}, "outbound": [], "sinks_after_loss": [],
             "first_funding": None, "holds": {}, "swept_assets_victim": sorted(swept_assets), "coin_raw": None, "complete": True, "notes": []}
        problems = 0                      # this profile's own failures; the shared gap list also carries the sibling's

        def sg(kind, detail):
            nonlocal problems
            problems += 1
            self.gap(kind, detail, cl.chain_id)
        try:
            # the three independent reads go out together (the explorer is 2-3 s per call)
            cl.check_budget()
            info, cnt, bal = cl.many([f"/addresses/{a}", f"/addresses/{a}/counters", f"/addresses/{a}/token-balances"])
            info, cnt = (info if isinstance(info, dict) else {}), (cnt if isinstance(cnt, dict) else {})
            s["label"], s["kind"] = _label(info)
            s["is_contract"] = info.get("is_contract")
            s["coin_raw"] = str(_int(info.get("coin_balance"))) if _int(info.get("coin_balance")) is not None else None
            s["tx_count"] = _int(cnt.get("transactions_count"))
            if s["kind"] == "bundler":
                # an ERC-4337 bundler relays other people's signed operations for a fee; its 100k+ transactions say nothing
                # about this case, and naming it as a suspect would point the reader at a service (shared service is not owner)
                s["service"] = True
                s["notes"].append("a public bundler (ERC-4337 relay service): it carried the transaction for whoever paid it; "
                                  "it is not a party to the loss and is not profiled further")
                s["loss_time_known"] = since_ts is not None
                s["balances_read"] = isinstance(bal, list)
                return s
            (txs, ok1), (tts, ok2), (its, ok3) = self.fanout([
                lambda: self.pages(cl, a, "transactions", PAGES_SUSPECT),
                lambda: self.pages(cl, a, "token-transfers", PAGES_SUSPECT),
                lambda: self.pages(cl, a, "internal-transactions", 1)])      # native value through contracts (relays, swaps)
            # type-4 txs this address submitted -> other authorities (candidate victims) and delegates
            t4 = [t for t in txs if str(t.get("type")) == "4" and _h(t.get("from")) == a]
            auths, dele = {}, {}
            fulls, ran_out, _ = self.fetch_txs(cl, [(t.get("hash") or "").lower() for t in t4[:T4_CAP]])
            if ran_out:
                sg("budget", f"suspect {a[:12]}: type-4 transactions only partly fetched")
            failed = [h for h, f in fulls.items() if f is None]
            if failed:
                sg("provider", f"suspect {a[:12]}: {len(failed)} type-4 transaction(s) could not be fetched")
            for full in fulls.values():
                for x in (full or {}).get("authorization_list") or []:
                    if isinstance(x, dict):
                        au = (x.get("authority") or "").lower()
                        auths[au] = auths.get(au, 0) + 1
                        d = (x.get("address_hash") or "").lower()
                        dele[d] = dele.get(d, 0) + 1
                for x in (full or {}).get("token_transfers") or []:
                    if _h(x.get("to")) == a and _h(x.get("from")) == self.victim:      # other authorities' losses are theirs, not this case's
                        raw, dec = _total(x)
                        ta = _tok_addr(_tok(x))
                        slot = s["swept_in"].setdefault(ta, {"symbol": _tok(x).get("symbol"), "raw": 0, "decimals": dec, "n": 0})
                        slot["raw"] += raw or 0
                        slot["n"] += 1
            s["swept_in"] = {k: {**v, "raw": str(v["raw"])} for k, v in s["swept_in"].items()}
            s["type4_submitted"] = len(t4)
            s["type4_fetched"] = sum(1 for f in fulls.values() if f is not None)
            if len(t4) > T4_CAP:
                sg("capped", f"suspect {a[:12]}: {len(t4)} type-4 transactions, {T4_CAP} fetched")
            others = {k: n for k, n in auths.items() if k and k != self.victim}
            s["other_authorities"] = len(others)
            s["authorities_sample"] = sorted(others, key=lambda k: -others[k])[:10]
            for d in sorted(dele, key=lambda k: -dele[k])[:2]:
                if not d or d == "0x" + "0" * 40:
                    s["delegates"].append({"addr": d or None, "uses": dele[d], "note": "delegation cleared" if d else None})
                    continue
                cl.check_budget()
                dinfo, code = cl.many([f"/addresses/{d}", f"/smart-contracts/{d}"])
                dinfo = dinfo if isinstance(dinfo, dict) else {}
                sha, ln = _code_hash(cl, d, code)
                s["delegates"].append({"addr": d, "uses": dele[d], "creator": (dinfo.get("creator_address_hash") or "").lower() or None,
                                       "verified": dinfo.get("is_verified"), "code_sha256": sha, "code_len": ln})
            # outbound: native (txs) + tokens; labels from the inline address objects (no extra calls). Each row counts how
            # many of its transfers happened at or after the first loss: only those can be proceeds of THIS loss.
            out = {}

            def slot_for(to_obj):
                return out.setdefault(_h(to_obj), {"addr": _h(to_obj), "label": _label(to_obj)[0], "kind": _label(to_obj)[1], "n": 0, "after_loss_n": 0,
                                                   "native_raw": 0, "assets": {}, "last": None, "transfers": [], "rehearsal": False})

            def after_loss(ts):
                t = _ts(ts)
                return since_ts is not None and t is not None and t >= since_ts   # unknown chronology counts nothing (loss_time_known)
            s["loss_time_known"] = since_ts is not None
            for t in [x for x in txs if x.get("status") in (None, "ok")] + [i for i in its if i.get("success") is not False]:
                if _h(t.get("from")) == a and _int(t.get("value")) and _h(t.get("to")):
                    r = slot_for(t.get("to"))
                    r["n"] += 1
                    r["after_loss_n"] += 1 if after_loss(t.get("timestamp")) else 0
                    r["native_raw"] += _int(t.get("value"))
                    r["last"] = max(r["last"] or "", t.get("timestamp") or "")
                    if NATIVE in swept_assets and after_loss(t.get("timestamp")) and len(r["transfers"]) < 8:    # swept coin, not just tokens
                        r["transfers"].append({"ts": t.get("timestamp"), "asset": NATIVE, "symbol": cl.chain.get("symbol") or "native", "raw": str(_int(t.get("value"))),
                                               "decimals": 18, "tx": (t.get("hash") or t.get("transaction_hash") or "").lower()})
            # proceeds: a canonical asset the suspect RECEIVED in the same tx it sent a swept asset (USDC swapped to DAI) is the
            # loot in a new coat and is followed exactly like the swept asset, by contract address. Missing this is why the
            # first automated pass on a real case saw no rehearsal sends: they were in DAI, the swap output.
            canon_set = set(self.canonical(cl.chain_id).values())
            swap_txs = {(x.get("transaction_hash") or "").lower() for x in tts if _h(x.get("from")) == a and _tok_addr(_tok(x)) in swept_assets}
            converted = {}
            for x in tts:
                ta = _tok_addr(_tok(x))
                if _h(x.get("to")) == a and (x.get("transaction_hash") or "").lower() in swap_txs and ta in canon_set and ta not in swept_assets:
                    converted[ta] = {"symbol": _tok(x).get("symbol"), "decimals": _total(x)[1]}
            s["converted"] = converted
            followed = set(swept_assets) | set(converted)
            trade_txs = {(x.get("transaction_hash") or "").lower() for x in tts if _h(x.get("to")) == a}   # the suspect got something back: a trade, not a payment
            for x in tts:
                if _h(x.get("from")) == a and _h(x.get("to")):
                    r = slot_for(x.get("to"))
                    r["n"] += 1
                    r["after_loss_n"] += 1 if after_loss(x.get("timestamp")) else 0
                    raw, dec = _total(x)
                    ta = _tok_addr(_tok(x))
                    slot = r["assets"].setdefault(ta, {"symbol": _tok(x).get("symbol"), "raw": 0, "decimals": dec, "canonical": ta in set(self.canonical(cl.chain_id).values())})
                    slot["raw"] += raw or 0
                    r["last"] = max(r["last"] or "", x.get("timestamp") or "")
                    if ta in followed and after_loss(x.get("timestamp")) and len(r["transfers"]) < 8 and (x.get("transaction_hash") or "").lower() not in trade_txs:   # by contract, never by symbol; a swap counterparty is not a hop
                        r["transfers"].append({"ts": x.get("timestamp"), "asset": ta, "symbol": _tok(x).get("symbol"), "raw": str(raw or 0),
                                               "decimals": dec, "tx": (x.get("transaction_hash") or "").lower()})
            for r in out.values():
                r["native_raw"] = str(r["native_raw"])
                r["assets"] = {k: {**v, "raw": str(v["raw"])} for k, v in r["assets"].items()}
            # the exit verdict looks at EVERY destination read, not the 15 shown
            s["sinks_after_loss"] = sorted(r["addr"] for r in out.values() if r["kind"] in ("exchange", "privacy_pool", "bridge") and r["after_loss_n"])
            s["outbound"] = sorted(out.values(), key=lambda r: (-r["after_loss_n"], -r["n"]))[:15]
            s["outbound_total"] = len(out)
            inb = [t for t in txs + its if _h(t.get("to")) == a and _int(t.get("value")) and t.get("success") is not False]
            if inb:
                f = min(inb, key=lambda t: t.get("timestamp") or "~")
                s["first_funding"] = {"ts": f.get("timestamp"), "from": _h(f.get("from")), "label": _label(f.get("from"))[0], "raw": str(_int(f.get("value"))),
                                      "via": "internal" if "transaction_hash" in f else "tx", "earliest_page_only": not ok1}
                if s["first_funding"]["from"] in out:
                    s["notes"].append("the address that first funded this one also RECEIVED funds back from it (a two-way flow with its funder: a lead that the two are related, not proof of one hand)")
            # balances for the swept assets (still holding?)
            if isinstance(bal, list):
                for b in bal:
                    if isinstance(b, dict):
                        ta = _tok_addr(_tok(b))
                        if ta in swept_assets or ta in s["swept_in"] or ta in converted:
                            s["holds"][ta] = {"symbol": _tok(b).get("symbol"), "raw": str(_int(b.get("value")) or 0), "decimals": _int(_tok(b).get("decimals"))}
                meta = swept_assets if isinstance(swept_assets, dict) else {}
                for ta in set(swept_assets) | set(s["swept_in"]) | set(converted):
                    known = s["swept_in"].get(ta) or meta.get(ta) or converted.get(ta) or {}
                    s["holds"].setdefault(ta, {"symbol": known.get("symbol"), "raw": "0", "decimals": known.get("decimals")})
                s["balances_read"] = True
            else:
                s["balances_read"] = False
                sg("provider", f"suspect {a[:12]}: token balances unavailable")
            # rehearsal sends: repeated tiny amounts of a swept asset to one address after the loss (1 then 10 DAI against a
            # 98,000 DAI balance) are consistent with testing a destination; a later large move is NOT established here. That address is the next
            # hop, profiled by hop(); services (bridges, exchanges, pools) are sinks, not hops.
            if NATIVE in swept_assets and s["coin_raw"] is not None:
                s["holds"][NATIVE] = {"symbol": cl.chain.get("symbol") or "native", "raw": s["coin_raw"], "decimals": 18}
            for r in out.values():
                small = []
                for t in r["transfers"]:
                    held = _int((s["holds"].get(t["asset"]) or {}).get("raw")) or 0
                    small.append(held > 0 and int(t["raw"]) * 100 <= held)
                r["rehearsal"] = len(small) >= 2 and all(small)
            s["rehearsal_any"] = any(r["rehearsal"] for r in out.values())        # over EVERY destination, not the 15 shown
            ranked = [r for r in sorted(out.values(), key=lambda r: (-int(r["rehearsal"]), -r["after_loss_n"])) if r["transfers"] and r["kind"] in ("eoa", "unknown")]
            s["next_hop_candidates"] = [r["addr"] for r in ranked]                  # uncapped; investigate() caps and records the gap
            s["next_hop_rows"] = {r["addr"]: {"transfers": r["transfers"], "rehearsal": r["rehearsal"]} for r in ranked}
            s["complete"] = ok1 and ok2 and ok3 and problems == 0
        except BudgetExceeded:
            self.exhausted = True
            s["complete"] = False
            sg("budget", f"budget exhausted while profiling suspect {a[:12]}")
        except _PROVIDER_ERRORS as e:
            s["complete"] = False
            sg("provider", f"suspect {a[:12]}: {type(e).__name__}: {str(e)[:100]}")
        return s

    def hop(self, cl, addr, origin, swept_assets, since_ts, transfers, rehearsal=False):
        """Profile a NEXT hop: an address a loss recipient paid after the loss. The question is who it is, not what it
        holds: its FIRST funding (pages walked back to the oldest; an exchange hot wallet there is the one record a legal
        request can reach), the labelled services it touched, whether it sent anything back to the recipient (two-way
        contact) and whether it passed the asset on. Every statement carries how far the pages were read."""
        a = addr.lower()
        h = {"addr": a, "chain_id": cl.chain_id, "origin": origin, "received": transfers, "rehearsal": bool(rehearsal), "label": None, "kind": None,
             "is_contract": None, "tx_count": None, "first_funding": None, "services": [], "contact_back": None, "forwarded": [],
             "pages_complete": False, "complete": False}
        try:
            cl.check_budget()
            info, cnt = cl.many([f"/addresses/{a}", f"/addresses/{a}/counters"])
            info, cnt = (info if isinstance(info, dict) else {}), (cnt if isinstance(cnt, dict) else {})
            h["label"], h["kind"] = _label(info)
            h["is_contract"] = info.get("is_contract")
            h["tx_count"] = _int(cnt.get("transactions_count"))
            n0 = len(self.gaps)
            (txs, ok1), (tts, ok2) = self.fanout([lambda: self.pages(cl, a, "transactions", HOP_PAGES),
                                                  lambda: self.pages(cl, a, "token-transfers", PAGES_SUSPECT)])
            h["pages_complete"] = ok1
            svc = {}
            for t in [x for x in txs if x.get("status") in (None, "ok")] + tts:     # token rows carry no status; failed txs are not contact
                for side in ("from", "to"):
                    o = t.get(side) if isinstance(t.get(side), dict) else {}
                    oa = _h(o)
                    if not oa or oa == a:
                        continue
                    text, kind = _label(o)
                    if kind in ("exchange", "bridge", "privacy_pool"):
                        slot = svc.setdefault(oa, {"addr": oa, "label": text, "kind": kind, "n": 0, "direction": set()})
                        slot["n"] += 1
                        slot["direction"].add("in" if side == "from" else "out")
                if _h(t.get("from")) == a and _h(t.get("to")) == origin and t.get("status") in (None, "ok") and h["contact_back"] is None:
                    h["contact_back"] = {"ts": t.get("timestamp"), "tx": (t.get("hash") or "").lower(), "raw": str(_int(t.get("value")) or 0)}
            h["services"] = sorted(({**v, "direction": sorted(v["direction"])} for v in svc.values()), key=lambda v: -v["n"])[:10]
            # first funding = the oldest incoming value on EITHER page type (a token credit can predate the first coin credit);
            # it is only "first" when both walks reached their oldest page
            inb = [t for t in txs if _h(t.get("to")) == a and _int(t.get("value")) and t.get("status") in (None, "ok")]
            inb += [x for x in tts if _h(x.get("to")) == a and (_total(x)[0] or 0) > 0]
            if inb:
                f = min(inb, key=lambda t: t.get("timestamp") or "~")
                text, kind = _label(f.get("from"))
                is_tok = "transaction_hash" in f
                h["first_funding"] = {"ts": f.get("timestamp"), "from": _h(f.get("from")), "label": text, "kind": kind,
                                      "raw": str(_total(f)[0] or 0) if is_tok else str(_int(f.get("value"))),
                                      "asset": _tok_addr(_tok(f)) if is_tok else NATIVE, "symbol": _tok(f).get("symbol") if is_tok else None,
                                      "tx": (f.get("transaction_hash") if is_tok else f.get("hash") or "").lower(), "earliest_page_only": not (ok1 and ok2)}
            for x in tts:
                ta = _tok_addr(_tok(x))
                if _h(x.get("from")) == a and ta in swept_assets and _h(x.get("to")) and len(h["forwarded"]) < 8:
                    ts = _ts(x.get("timestamp"))
                    if since_ts is None or (ts is not None and ts >= since_ts):
                        raw, dec = _total(x)
                        text, kind = _label(x.get("to"))
                        h["forwarded"].append({"ts": x.get("timestamp"), "to": _h(x.get("to")), "label": text, "kind": kind, "symbol": _tok(x).get("symbol"),
                                               "raw": str(raw or 0), "decimals": dec, "tx": (x.get("transaction_hash") or "").lower()})
            h["complete"] = ok1 and ok2 and len(self.gaps) == n0
        except BudgetExceeded:
            self.exhausted = True
            self.gap("budget", f"budget exhausted while profiling next hop {a[:12]}", cl.chain_id)
        except _PROVIDER_ERRORS as e:
            self.gap("provider", f"next hop {a[:12]}: {type(e).__name__}: {str(e)[:100]}", cl.chain_id)
        return h

    @staticmethod
    def relate(suspects):
        """Cross-suspect relations stated from the chain only: same delegate code (exact or same length+creator pattern)."""
        rel = []
        seen = {}
        for s in suspects:
            for d in s["delegates"]:
                if d.get("code_len"):
                    seen.setdefault(d["code_len"], []).append((s["addr"], d["addr"], d.get("code_sha256")))
        for ln, lst in seen.items():
            if len({x[0] for x in lst}) > 1:
                exact = len({x[2] for x in lst}) == 1
                rel.append({"kind": "same_delegate_code" if exact else "same_delegate_code_length",
                            "detail": f"{len(lst)} delegate contracts of {ln} bytes used by {len({x[0] for x in lst})} suspects" + ("" if exact else " (same size; differs only in embedded data such as a constructor-set address)"),
                            "members": sorted({x[0] for x in lst}), "evidence_level": "strong" if exact else "medium"})
        return rel


_MAX_UINT = 2**256 - 1


def _self_authorised(recs, prof, incident_ts, window_s=3600):
    """Mark third-party rows that followed this wallet's OWN exact-amount approval of the same token within `window_s`:
    approve exactly N, then a relayer or router pulls exactly N is the shape of a swap or bridge the owner ordered.
    Such rows are kept and shown but not counted as loss. A delegation sweep is never excused this way, and an
    unlimited approval excuses nothing. Rows more than an hour before the reported incident time stay counted (the
    reported time can be wrong) but are tagged for the reader. Pure computation over pages already read."""
    aps = []
    for ap in prof.get("approvals") or []:
        try:
            val, t = int(str(ap.get("value") or "")), _ts(ap.get("ts"))
        except ValueError:
            continue
        if t is not None and 0 < val < _MAX_UINT:
            aps.append((ap.get("token"), val, t, ap))
    for r in recs:
        if r.get("mechanism") not in THIRD_PARTY:
            continue
        ts = _ts(r.get("ts"))
        if incident_ts and ts is not None and ts < incident_ts - window_s:
            r["before_reported_incident"] = True
        if r.get("mechanism") in SWEEP_LIKE or ts is None:
            continue
        sums = {}                                  # asset -> total that left in this tx (a swap's output and its fee are two rows of one order)
        for o in r.get("outflows", []):
            try:
                sums[o.get("asset")] = sums.get(o.get("asset"), 0) + int(o.get("raw") or 0)
            except (TypeError, ValueError):
                continue
        for asset, raw in sums.items():
            hit = next((ap for tok, val, t, ap in aps if tok == asset and val == raw and 0 <= ts - t <= window_s), None)
            if hit:
                r["self_authorised"] = True
                r["self_authorised_reason"] = (f"{int((ts - (_ts(hit.get('ts')) or ts)) // 60)} min after this wallet's own approval of exactly this "
                                               f"amount to {(hit.get('spender') or '?')[:12]} (tx {(hit.get('tx') or '')[:12]}): the shape of a swap "
                                               "or bridge the wallet ordered, so not counted as loss")
                break


def _delegation_origin(dele, recs):
    """If the delegate the account points at RIGHT NOW is the one a third-party sweep installed, say so: a verified
    vendor name does not make that delegation the owner's. Records the loss tx and its submitter on the delegation."""
    if not isinstance(dele, dict) or not dele.get("delegated"):
        return
    target = dele.get("delegate")
    hit = next((r for r in recs if r.get("mechanism") == "eip7702_sweep"
                and any(a.get("delegate") == target for a in r.get("authorizations") or [])), None)
    dele["set_in_loss_tx"] = hit["tx"] if hit else None
    dele["set_by"] = hit.get("submitter") if hit else None


def investigate(client, victim, incident_ts=None, chains=None, max_chains=2):
    """Entry point. `client` is the case's chain; `chains` = {chain_id(str): Client} for every chain to check (defaults to
    every configured chain with an explorer, sharing the case client's deadline). Returns a JSON-able dict; never raises
    on provider failure or budget expiry: partial results carry their gaps."""
    victim = victim.lower()
    if chains is None:
        chains = {str(client.chain_id): client}
        cfg = getattr(client, "cfg", None) or {}
        for cid, ch in (cfg.get("chains") or {}).items():
            if str(cid) not in chains and ch.get("explorer"):
                try:
                    sib = Client(cfg, cid, cache=True)
                    sib.deadline = client.deadline
                    chains[str(cid)] = sib
                except (SystemExit, _PROVIDER_ERRORS):
                    continue
    inv = _Investigation(client, victim, incident_ts, chains)
    rem = client.remaining()
    if rem is not None:
        inv.soft_deadline = time.monotonic() + max(0.0, rem) * (1 - SUSPECT_SHARE)
    out = {"rule_version": RULE_VERSION, "collector_version": COLLECTOR_VERSION, "victim": victim, "incident_ts": incident_ts,
           "presence": {}, "delegations": {}, "chains": [], "quarantine_total": 0, "suspects": [], "next_hops": [], "relations": [], "verdict": {}, "gaps": inv.gaps,
           "complete": False, "observed_at": time.time(), "timing_s": {}}
    t0 = time.monotonic()

    def lap(stage):
        out["timing_s"][stage] = round(time.monotonic() - t0, 1)      # cumulative seconds; for tuning the budget, not evidence
    try:
        out["presence"] = inv.presence()
        lap("presence")
        # the delegation check runs on EVERY chain the explorer knows the address on, busy or not: a delegated account
        # with an empty balance and stale counters is still an open door, and it is the cheapest read here
        def dele_one(cid):
            p = out["presence"].get(cid) or {}
            if p.get("seen") is False:
                return {"known": True, "delegated": False, "delegate": None}       # never seen: nothing can be delegated
            if p.get("seen") is None:
                return {"known": False, "delegated": None, "delegate": None}
            try:
                return inv.delegation(chains[cid])
            except BudgetExceeded:
                inv.exhausted = True
                inv.gap("budget", f"budget exhausted before the delegation check on chain {cid}", cid)
                return {"known": False, "delegated": None, "delegate": None}
        seen_cids = [cid for cid in chains if cid in out["presence"]]
        out["delegations"] = dict(zip(seen_cids, inv.fanout([lambda cid=cid: dele_one(cid) for cid in seen_cids]))) if seen_cids else {}
        lap("delegation")
        case_cid = str(client.chain_id)
        active = sorted((cid for cid, p in out["presence"].items() if (p.get("tx") or 0) + (p.get("transfers") or 0) > 0),
                        key=lambda cid: (cid != case_cid, -((out["presence"][cid].get("tx") or 0) + (out["presence"][cid].get("transfers") or 0))))
        if len(active) > max_chains:
            inv.gap("capped", f"activity on {len(active)} chains; {max_chains} read (the case chain first, then the busiest): " + ", ".join(active[:max_chains]))
            inv.chains_unread.update(active[max_chains:])
        recv_assets, candidates = {}, []           # (recipient, chain_id) -> this victim's assets that reached it in a loss tx; (addr, chain, weight, role)
        first_loss = {}                            # chain_id -> epoch of the earliest loss transaction read there
        for rank, cid in enumerate(active[:max_chains]):
            if inv.exhausted:
                inv.gap("budget", f"chain {cid} not read: budget exhausted", cid)
                inv.chains_unread.add(cid)
                continue
            if rank > 0 and candidates and inv.past_soft():
                inv.gap("capped", f"chain {cid} not read: the remaining time is kept for profiling the suspects already found", cid)
                inv.chains_unread.add(cid)
                continue
            cl = chains[cid]
            try:
                invt = inv.inventory(cl, PAGES_PRIMARY if rank == 0 else PAGES_SECONDARY)
            except BudgetExceeded:
                inv.exhausted = True
                inv.gap("budget", f"budget exhausted while reading chain {cid}", cid)
                inv.chains_unread.add(cid)
                continue
            except _PROVIDER_ERRORS as e:
                inv.gap("provider", f"chain {cid}: {type(e).__name__}: {str(e)[:100]}", cid)
                inv.chains_unread.add(cid)
                continue
            dele = out["delegations"].get(cid) or {"known": False, "delegated": None, "delegate": None}
            # the per-transaction fetches keep whatever they managed when the budget runs out (never thrown away with the chain)
            try:
                recs, fetched_all = inv.mechanisms(cl, invt) if not inv.exhausted else ([], False)
            except BudgetExceeded:
                inv.exhausted, recs, fetched_all = True, [], False
                inv.gap("budget", f"budget exhausted while classifying outflows on chain {cid}", cid)
            except _PROVIDER_ERRORS as e:
                recs, fetched_all = [], False
                inv.gap("provider", f"chain {cid} mechanisms: {type(e).__name__}: {str(e)[:100]}", cid)
            if inv.exhausted and not fetched_all:
                inv.gap("budget", f"outflows on chain {cid} only partly classified", cid)
            prof = inv.profile(cl, invt)      # pure computation over pages already read
            _self_authorised(recs, prof, incident_ts)
            _delegation_origin(dele, recs)
            lap(f"chain_{cid}")
            # third-party rows more than an hour before the reported time are the wallet's earlier history (routine permit
            # or allowance use), listed with their own total and not summed as the loss; the page says to re-run with an
            # earlier time if the loss began before the time given
            third = [r for r in recs if r.get("mechanism") in THIRD_PARTY and not r.get("self_authorised") and not r.get("before_reported_incident")]
            earlier = [r for r in recs if r.get("mechanism") in THIRD_PARTY and not r.get("self_authorised") and r.get("before_reported_incident")]
            self_signed = [r for r in recs if r.get("mechanism") in SELF_SIGNED]
            # a key-signed outflow is tied to the loss when it went to an UNLABELLED party of a delegation sweep (a router,
            # pool or relayer shared with a sweep is a shared service, not a tie), within 10 min of such a sweep, or to a
            # look-alike of a real counterparty (the poisoning payoff)
            sweep = [r for r in third if r.get("mechanism") in SWEEP_LIKE]
            sweep_parties = {r["submitter"] for r in sweep} | {x for r in sweep for x in r.get("recipients", [])}
            sweep_times = [_ts(r.get("ts")) for r in sweep if _ts(r.get("ts"))]
            for r in self_signed:
                rows = r.get("outflows", [])
                tied = any(o.get("to") in sweep_parties and not o.get("to_label") for o in rows)
                bait = any(o.get("lookalike_recipient") for o in rows)
                ts = _ts(r.get("ts"))
                near = ts is not None and any(abs(ts - st) <= 600 for st in sweep_times)
                r["tied_to_loss"] = bool(tied or near or bait)
                r["tie_reason"] = ("recipient is a sweep party" if tied else "within 10 minutes of a third-party sweep" if near
                                   else "sent to a look-alike of a real counterparty (address poisoning payoff)" if bait else None)
            losses = third + [r for r in self_signed if r.get("tied_to_loss")]
            if losses:
                first_loss[cid] = min((_ts(r.get("ts")) for r in losses if _ts(r.get("ts"))), default=None)
            for r in losses:
                for o in r.get("outflows", []):
                    if o["asset"] != NATIVE and o.get("to"):
                        recv_assets.setdefault((o["to"], cid), {}).setdefault(o["asset"], {"symbol": o.get("symbol"), "decimals": o.get("decimals")})
                w = _WEIGHT.get(r.get("mechanism"), 3)      # key-signed-but-tied rows weigh 3
                for x in r.get("recipients", []):
                    candidates.append((x, cid, w, "recipient"))
                if r.get("mechanism") in THIRD_PARTY and r.get("submitter") and r["submitter"] != victim:
                    candidates.append((r["submitter"], cid, w, "submitter"))
            totals, earlier_totals = {}, {}
            for rows, sink in ((losses, totals), (earlier, earlier_totals)):
                for r in rows:
                    for o in r.get("outflows", []):
                        slot = sink.setdefault(o["asset"], {"symbol": o.get("symbol"), "raw": 0, "decimals": o.get("decimals"), "canonical": o.get("canonical"), "n": 0})
                        slot["raw"] += int(o["raw"] or 0)
                        slot["n"] += 1
            out["chains"].append({
                "chain_id": cid, "name": cl.chain.get("name"), "explorer": getattr(cl, "explorer", None),
                "pages_complete": invt["pages_complete"], "oldest_read": invt["oldest_read"],
                "outflow_txs": len({o["tx"] for o in invt["outflows"]}), "outflows_fetched_all": fetched_all,
                "mechanisms": recs, "losses": losses, "loss_totals": {k: {**v, "raw": str(v["raw"])} for k, v in totals.items()},
                "self_signed_untied": [r for r in self_signed if not r.get("tied_to_loss")][:50],
                "earlier_third_party": earlier[:50], "earlier_totals": {k: {**v, "raw": str(v["raw"])} for k, v in earlier_totals.items()},
                "third_party_self_authorised": [r for r in recs if r.get("self_authorised")][:50],
                "quarantine": invt["quarantine"][:60], "quarantine_total": len(invt["quarantine"]),
                "credits_read": len(invt["credits"]), "profile": prof, "current_delegation": dele,
            })
            out["quarantine_total"] += len(invt["quarantine"])
        # suspects: weighted by the mechanism that named them (a delegation sweep outranks ten unresolved dust rows), and
        # the strongest party on every chain read is profiled before the second-strongest on any chain
        counts, weight, roles = {}, {}, {}
        for a, cid, w, role in candidates:
            if a and a != victim:
                counts[(a, cid)] = counts.get((a, cid), 0) + 1
                weight[(a, cid)] = weight.get((a, cid), 0) + w
                roles.setdefault((a, cid), set()).add(role)
        by_weight = sorted(counts, key=lambda k: (-weight[k], -counts[k]))
        ordered = []
        for cid in active[:max_chains]:
            best = next((k for k in by_weight if k[1] == cid), None)
            if best:
                ordered.append(best)
        ordered += [k for k in by_weight if k not in ordered]
        if len(ordered) > SUSPECT_CAP:
            inv.gap("capped", f"{len(ordered)} addresses received or submitted loss transactions; the {SUSPECT_CAP} strongest-linked were profiled")
            inv.suspects_unprofiled = True
        todo = ordered[:SUSPECT_CAP]
        for i in range(0, len(todo), SUSPECT_PAIR):      # two at a time: each profile is ~10 explorer calls
            pair = todo[i:i + SUSPECT_PAIR]
            if inv.exhausted:
                for a, cid in pair:
                    inv.gap("budget", f"suspect {a[:12]} not profiled: budget exhausted", cid)
                inv.suspects_unprofiled = True
                continue
            profiles = inv.fanout([lambda a=a, cid=cid: inv.suspect(chains[cid], a, recv_assets.get((a, cid), {}), first_loss.get(cid), roles[(a, cid)])
                                   for a, cid in pair])
            for (a, cid), s in zip(pair, profiles):
                s["loss_txs"] = counts[(a, cid)]
                out["suspects"].append(s)
            lap("suspects_" + "_".join(a[:10] for a, _ in pair))
        # next hops: the addresses a recipient paid after the loss, rehearsal sends first, profiled from their own pages back to
        # their FIRST funding. The loot address was seeded through a privacy pool; the address it pays may have been funded by
        # an exchange withdrawal, the one record outside the chain. Found by hand first (2026-10-27); now part of every run.
        hop_todo = []
        for s in out["suspects"]:
            for a in s.get("next_hop_candidates") or []:
                if a != victim and all((a, s["chain_id"]) != (x[0], x[1]) for x in hop_todo):
                    row = (s.get("next_hop_rows") or {}).get(a) or {}
                    hop_todo.append((a, s["chain_id"], s["addr"], row.get("transfers") or [], row.get("rehearsal", False), set(s.get("converted") or {})))
        if len(hop_todo) > HOP_CAP:
            inv.gap("capped", f"{len(hop_todo)} next-hop candidates; the {HOP_CAP} strongest (rehearsal sends first) were profiled")
            inv.hops_unprofiled = True             # an unprofiled candidate could carry the exchange: negatives become unknown
            hop_todo = hop_todo[:HOP_CAP]
        for i in range(0, len(hop_todo), SUSPECT_PAIR):
            pair = hop_todo[i:i + SUSPECT_PAIR]
            if inv.exhausted:
                for a, cid, *_ in pair:
                    inv.gap("budget", f"next hop {a[:12]} not profiled: budget exhausted", cid)
                inv.hops_unprofiled = True
                continue
            out["next_hops"] += inv.fanout([lambda a=a, cid=cid, o=o, tr=tr, rh=rh, cv=cv: inv.hop(chains[cid], a, o, set(recv_assets.get((o, cid), {})) | cv, first_loss.get(cid), tr, rh)
                                            for a, cid, o, tr, rh, cv in pair])
            lap("hops_" + "_".join(a[:10] for a, *_ in pair))
        # a submitter that is a relay service is named as one wherever the delegation panel cites it
        kinds = {(s["addr"], s["chain_id"]): (s.get("kind"), s.get("label")) for s in out["suspects"]}
        for cid, d in out["delegations"].items():
            if isinstance(d, dict) and d.get("set_by") and (d["set_by"], cid) in kinds:
                d["set_by_kind"], d["set_by_label"] = kinds[(d["set_by"], cid)]
        out["relations"] = inv.relate(out["suspects"])
    except BudgetExceeded:
        inv.exhausted = True
        inv.gap("budget", "budget exhausted")
    except _PROVIDER_ERRORS as e:
        inv.gap("provider", f"{type(e).__name__}: {str(e)[:120]}")
    out["verdict"] = _verdicts(out, inv)
    out["complete"] = inv.complete and not inv.exhausted and bool(out["chains"]) and all(c["pages_complete"] and c["outflows_fetched_all"] for c in out["chains"]) \
        and all(s["complete"] for s in out["suspects"])
    out["observed_at"] = time.time()
    return out


def _verdicts(out, inv):
    """Three-valued, separate questions. True on positive evidence; False only when every configured chain answered the
    presence check, every active chain was read in full (none capped, skipped or failed) and every suspect of the relevant
    role was profiled completely."""
    chains = out["chains"]
    loss_recs = [r for c in chains for r in c["mechanisms"] if not r.get("self_authorised") and not r.get("before_reported_incident")]
    mechs = [r.get("mechanism") for r in loss_recs]
    presence_ok = bool(out["presence"]) and set(out["presence"]) >= set(inv.chains) and all(p.get("tx") is not None for p in out["presence"].values())
    read_ok = presence_ok and not inv.chains_unread and not inv.exhausted and all(c["pages_complete"] for c in chains)
    fetched_ok = read_ok and all(c["outflows_fetched_all"] for c in chains) and None not in mechs
    any_out = any(c["outflow_txs"] for c in chains)
    deles = out.get("delegations") or {}

    def tv(found, ok):
        return True if found else (False if ok else None)
    v = {
        "assets_left_wallet": tv(any_out, read_ok),
        "eip7702_authorization_by_wallet_submitted_by_other": tv("eip7702_sweep" in mechs, fetched_ok),
        "allowance_or_permit_used_by_third_party": tv(any(m in ("allowance_used_by_spender", "permit2_signature_used", "permit_signature_used") for m in mechs), fetched_ok),
        "wallet_key_signed_outflow": tv(any(m in SELF_SIGNED for m in mechs), fetched_ok),
        "third_party_tx_unresolved": tv("third_party_tx_unresolved" in mechs, fetched_ok),
        "sweep_followed_inbound_credit": tv(any(r.get("after_credit_s") is not None for r in loss_recs), fetched_ok),
        "activity_on_other_chains": tv(len([1 for cid, p in out["presence"].items() if (p.get("tx") or 0) + (p.get("transfers") or 0) > 0]) > 1, presence_ok),
        # a live delegation is the open door the tweet-era wallets miss: approvals revoked, delegate still in place.
        # Checked on every chain the explorer knows the address on, not only the busy ones.
        "wallet_currently_delegated": tv(any(d.get("delegated") for d in deles.values()),
                                         presence_ok and set(deles) >= set(inv.chains) and all(d.get("known") for d in deles.values())),
    }
    sus = out["suspects"]
    if sus or inv.suspects_unprofiled:
        # each question is asked only of the parties whose role can answer it; "no" needs every such party read in full
        # and no candidate left unprofiled (cap or budget)
        subs = [s for s in sus if "submitter" in (s.get("roles") or []) and not s.get("service")]   # a bundler answers nothing
        recs = [s for s in sus if "recipient" in (s.get("roles") or []) and not s.get("service")]
        all_prof = not inv.suspects_unprofiled
        svc_rec = any(s.get("service") and "recipient" in (s.get("roles") or []) for s in sus)   # a service that RECEIVED answers no recipient question

        def ok(group):
            return all_prof and bool(group) and all(s["complete"] for s in group)
        recs_ok = ok(recs) and not svc_rec
        v["other_wallets_authorized_to_same_submitter"] = tv(any((s.get("other_authorities") or 0) > 0 for s in subs), ok(subs))
        holds = [int(h["raw"]) > 0 for s in recs for ta, h in s["holds"].items() if ta in (s.get("swept_assets_victim") or [])]
        v["recipient_still_holds_swept_asset"] = tv(any(holds), recs_ok and all(s.get("balances_read") for s in recs))
        v["recipient_sent_to_exchange_bridge_or_privacy_pool"] = tv(any(s.get("sinks_after_loss") for s in recs),
                                                                    recs_ok and all(s.get("loss_time_known") for s in recs))
        v["recipient_rehearsal_sends"] = tv(any(s.get("rehearsal_any") for s in recs),
                                            recs_ok and all(s.get("loss_time_known") and s.get("balances_read") for s in recs))
        hops = out.get("next_hops") or []
        if hops or inv.hops_unprofiled:
            hops_ok = not inv.hops_unprofiled and bool(hops) and all(h.get("complete") for h in hops)
            walked = [h.get("first_funding") or {} for h in hops if h.get("first_funding") and not h["first_funding"].get("earliest_page_only")]
            # a "first" funder is only known when the walk reached the oldest page: an older, unseen credit could exist otherwise
            v["next_hop_first_funded_by_exchange"] = tv(any(f.get("kind") == "exchange" for f in walked), hops_ok and len(walked) == len(hops))
            v["next_hop_touched_exchange"] = tv(any(sv.get("kind") == "exchange" for h in hops for sv in h.get("services") or []), hops_ok)
    return v


def explain_tx(client, tx_hash):
    """Single-transaction explainer: roles, authorizations, transfers, and the mechanism for every account whose
    assets left in it. Says what one transaction can and cannot establish."""
    h = (tx_hash or "").lower()
    out = {"rule_version": RULE_VERSION, "collector_version": COLLECTOR_VERSION, "chain_id": client.chain_id, "tx": h, "found": False,
           "gaps": [], "accounts": [], "complete": False, "observed_at": time.time()}
    errs = getattr(client, "errors", None)
    n0 = len(errs) if isinstance(errs, list) else 0      # the client turns a soft explorer failure into None + an error entry
    try:
        full = client.tx(h)
    except BudgetExceeded:
        out["gaps"].append({"kind": "budget", "detail": "budget exhausted before the transaction was read"})
        return out
    except _PROVIDER_ERRORS as e:
        out["gaps"].append({"kind": "provider", "detail": f"{type(e).__name__}: {str(e)[:120]}"})
        return out
    if not isinstance(full, dict):
        if isinstance(errs, list) and len(errs) > n0:
            out["gaps"].append({"kind": "provider", "detail": f"the explorer failed to answer for this hash: {str(errs[-1])[:100]}"})
        else:
            out["gaps"].append({"kind": "not_found", "detail": "the explorer has no confirmed transaction with this hash on this chain (wrong chain, pending, or mistyped)"})
        return out
    out["found"] = True
    dec = full.get("decoded_input") if isinstance(full.get("decoded_input"), dict) else {}
    out.update({"ts": full.get("timestamp"), "block": _int(full.get("block_number")), "status": full.get("status"), "type": str(full.get("type")) if full.get("type") is not None else None,
                "submitter": _h(full.get("from")), "submitter_label": _label(full.get("from"))[0], "to": _h(full.get("to")), "to_label": _label(full.get("to"))[0],
                "method": str(full.get("method") or dec.get("method_call") or "")[:80] or None,
                "value_raw": str(_int(full.get("value")) or 0), "fee_raw": str(_int((full.get("fee") or {}).get("value")) or 0) if isinstance(full.get("fee"), dict) else None,
                "authorizations": [{"authority": (a.get("authority") or "").lower(), "delegate": (a.get("address_hash") or "").lower(), "nonce": _int(a.get("nonce"))}
                                   for a in (full.get("authorization_list") or []) if isinstance(a, dict)]})
    canon = {str(k).upper(): str(v).lower() for k, v in ((getattr(client, "cfg", None) or {}).get("chains", {}).get(str(client.chain_id), {}).get("canonical_tokens") or {}).items()}
    transfers = []
    for x in full.get("token_transfers") or []:
        if not isinstance(x, dict):
            continue
        raw, dec = _total(x)
        tok = _tok(x)
        sym, odd = _sym_ascii(tok.get("symbol"))
        ta = _tok_addr(tok)
        transfers.append({"from": _h(x.get("from")), "to": _h(x.get("to")), "to_label": _label(x.get("to"))[0], "token": ta, "symbol": tok.get("symbol"),
                          "raw": str(raw) if raw is not None else None, "decimals": dec, "canonical": ta in set(canon.values()),
                          "suspicious": odd or sym == str(client.chain.get("symbol", "ETH")).upper() or (sym in IMPERSONATED and sym in canon and ta != canon[sym]) or bool(tok.get("is_scam"))})
    if full.get("token_transfers_overflow"):
        out["gaps"].append({"kind": "truncated", "detail": "the explorer truncated this transaction's token transfer list"})
    n1 = len(errs) if isinstance(errs, list) else 0
    try:
        internal = client.tx_internal(h) or []
    except BudgetExceeded:
        internal = []
        out["gaps"].append({"kind": "budget", "detail": "internal transfers not read (budget)"})
    except _PROVIDER_ERRORS:
        internal = []
        out["gaps"].append({"kind": "provider", "detail": "internal transfers unavailable"})
    if isinstance(errs, list) and len(errs) > n1:
        out["gaps"].append({"kind": "provider", "detail": "internal transfers unavailable (the client returned an empty list on a failed read)"})
    native = []
    for i in internal:
        if isinstance(i, dict) and _int(i.get("value")) and i.get("success") is not False:
            native.append({"from": _h(i.get("from")), "to": _h(i.get("to")), "raw": str(_int(i.get("value"))), "type": i.get("type")})
    if _int(full.get("value")) and full.get("status") in (None, "ok"):       # a reverted call moves no value
        native.append({"from": out["submitter"], "to": out["to"], "raw": str(_int(full.get("value"))), "type": "call"})
    out["token_transfers"], out["native_transfers"] = transfers, native
    affected = {t["from"] for t in transfers if t["from"] and not t["suspicious"]} | {n["from"] for n in native if n["from"]} | {a["authority"] for a in out["authorizations"] if a["authority"]}
    affected.discard("")
    for acct in sorted(affected):
        assets = {t["token"] for t in transfers if t["from"] == acct}
        rec = _Investigation.classify(full, acct, assets)
        rec["outflows"] = [t for t in transfers if t["from"] == acct] + [n for n in native if n["from"] == acct]
        rec["recipients"] = sorted({t["to"] for t in rec["outflows"] if t.get("to")})
        rec["account"] = acct
        rec["assets_left"] = any(not o.get("suspicious") for o in rec["outflows"])   # an authorization or a fake token alone moves nothing real
        out["accounts"].append(rec)
    out["complete"] = not any(g["kind"] in ("truncated", "provider", "budget") for g in out["gaps"])
    out["limits"] = ("One transaction shows who submitted it, who signed any EIP-7702 authorization, what moved and to whom. It cannot show earlier "
                     "allowances, whether a signature was obtained by deception, the full loss, how a key was obtained, who controls the recipient, "
                     "or what happened to the assets afterwards; run a wallet investigation for those.")
    return out
