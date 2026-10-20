"""Contract quick-audit: verification, proxy/upgradeability, privileged write functions, recent admin calls."""
RISKY = ("mint", "blacklist", "setfee", "settax", "pause", "upgrade", "withdraw", "rescue", "sweep", "setowner",
         "transferownership", "setadmin", "setmax", "exclude", "setswap", "setrouter", "setcollector", "setfeecollector",
         "renounce", "burnfrom", "settrading", "enabletrading", "setlimits", "setbot", "setsell", "setbuy")


def audit(client, addr):
    meta = client.address(addr)
    out = {"addr": addr, "is_contract": meta.get("is_contract"), "name": meta.get("name"), "verified": meta.get("is_verified"),
           "proxy_type": meta.get("proxy_type"), "implementations": meta.get("implementations"),
           "creator": meta.get("creator_address_hash"), "creation_tx": meta.get("creation_transaction_hash"),
           "scam_flag": meta.get("is_scam"), "flags": []}
    if not meta.get("is_contract"):
        out["flags"].append("not a contract")
        return out
    sc = client.get(f"/smart-contracts/{addr}", soft=True) or {}
    abi = sc.get("abi") or []
    writes = [f["name"] for f in abi if f.get("type") == "function" and f.get("stateMutability") not in ("view", "pure")]
    out["write_functions"] = writes
    out["privileged_functions"] = [w for w in writes if any(k in w.lower() for k in RISKY)]
    out["compiler"] = sc.get("compiler_version")
    out["license"] = sc.get("license_type")
    out["source_chars"] = len(sc.get("source_code") or "")
    if not meta.get("is_verified"):
        out["flags"].append("UNVERIFIED source: cannot audit logic")
    if meta.get("proxy_type") and meta.get("proxy_type") != "eip7702":
        out["flags"].append(f"upgradeable proxy ({meta['proxy_type']}): logic can change")
    if any("mint" in w.lower() for w in writes):
        out["flags"].append("mint function present")
    if any("blacklist" in w.lower() or "setbot" in w.lower() for w in writes):
        out["flags"].append("blacklist capability")
    src = (sc.get("source_code") or "").lower()
    for marker, msg in (("selfdestruct", "selfdestruct in source"), ("delegatecall", "delegatecall in source"),
                        ("tx.origin", "tx.origin check in source")):
        if marker in src:
            out["flags"].append(msg)
    txs, _ = client.paginate(f"/addresses/{addr}/transactions", max_pages=1)
    admin_calls = [{"ts": x["timestamp"], "method": x.get("method"), "from": (x.get("from") or {}).get("hash"), "tx": x["hash"], "status": x.get("status")}
                   for x in txs if x.get("method") and any(k in x["method"].lower() for k in RISKY)]
    out["recent_privileged_calls"] = admin_calls[:10]
    return out
