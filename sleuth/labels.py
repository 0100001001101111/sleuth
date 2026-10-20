"""Address classification. A service (bridge, router, pool, launchpad) is never evidence of common ownership."""
DEAD = {"0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000"}


def classify(meta, counters, cfg):
    """Return (kind, label). kind in eoa|contract|service|dead|unknown."""
    a = (meta.get("hash") or "").lower()
    if a in DEAD:
        return "dead", "dead address"
    known = cfg["known_services"].get(a)
    if known:
        return "service", known
    name = meta.get("name") or ""
    low = name.lower()
    tx = (counters or {}).get("tx", 0)
    if meta.get("is_contract"):
        if meta.get("proxy_type") == "eip7702":
            return "eoa", "EIP-7702 delegated wallet"
        if any(m in low for m in cfg["service_name_markers"]) or tx >= cfg["service_tx_threshold"]:
            return "service", name or "high-volume contract"
        return "contract", name or "contract"
    if meta.get("is_contract") is None:
        return "unknown", "unresolved"
    if tx >= cfg["service_tx_threshold"]:
        return "service", f"high-volume EOA ({tx:,} txs; bridge solver / exchange hot wallet)"
    return "eoa", "wallet"
