"""Decode PonsV2LaunchAndBuy.launchAndBuy calldata (verified source on Blockscout, 2026-10-26):
launchAndBuy(params(name,symbol,logo,description,socials,creatorFeeRecipient,creatorTaxBps,buybackEnabled,
expectedEconomics,salt), launchConfigId, pairToken, quoteIn, minTokensOut, recipient, address[] snipeTaxExemptions).
The exemption list is the ring signature from the $18.4M Pons case (creators waived the 99% snipe tax for up to 32
wallets); 41% of launches in a measured 8 h window used it, so it is an INPUT to a heuristic, not a verdict."""
from ..client import ApiError, BudgetExceeded

SELECTOR = "0xf85f8e41"
MAX_EXEMPTIONS = 4096   # sanity bound against hostile calldata


def _word(d, i):
    w = d[32 * i:32 * i + 32]
    if len(w) != 32:
        raise ValueError(f"calldata too short at word {i}")
    return int.from_bytes(w, "big")


def _addr(d, off):
    if off + 32 > len(d):
        raise ValueError("address word out of range")
    return "0x" + d[off + 12:off + 32].hex()


def _string(d, base, i):
    off = base + _word(d[base:], i)
    if off + 32 > len(d):
        raise ValueError("string offset out of range")
    n = int.from_bytes(d[off:off + 32], "big")
    if n > 4096 or off + 32 + n > len(d):
        raise ValueError("string length out of range")
    return d[off + 32:off + 32 + n].decode("utf-8", errors="replace")


def decode_launch_and_buy(input_hex):
    """-> dict or None when the selector does not match. Raises ValueError on malformed calldata."""
    if not isinstance(input_hex, str) or not input_hex.lower().startswith(SELECTOR):
        return None
    d = bytes.fromhex(input_hex[10:])
    p_off = _word(d, 0)
    out = {"launch_config_id": _word(d, 1), "pair_token": _addr(d, 32 * 2), "quote_in_raw": _word(d, 3),
           "min_tokens_out_raw": _word(d, 4), "recipient": _addr(d, 32 * 5)}
    ex_off = _word(d, 6)
    n = _word(d[ex_off:], 0)
    if n > MAX_EXEMPTIONS or ex_off + 32 + 32 * n > len(d):
        raise ValueError("exemption array out of range")
    out["snipe_tax_exemptions"] = [_addr(d, ex_off + 32 + 32 * i) for i in range(n)]
    if p_off + 32 * 10 > len(d):
        raise ValueError("params tuple out of range")
    out["name"] = _string(d, p_off, 0)
    out["symbol"] = _string(d, p_off, 1)
    out["creator_fee_recipient"] = _addr(d, p_off + 32 * 5)
    out["creator_tax_bps"] = _word(d[p_off:], 6)
    out["buyback_enabled"] = _word(d[p_off:], 7) != 0
    return out


def launch_calldata(client, creation_tx):
    """Fetch + decode a launch tx. {found, decoded, sender, launcher_contract, gaps}. Never raises on provider failure."""
    out = {"tx": creation_tx, "found": False, "decoded": None, "sender": None, "launcher_contract": None, "gaps": []}
    errs = []
    try:
        (tx,) = client.rpc([("eth_getTransactionByHash", [creation_tx])], errors=errs)
    except (BudgetExceeded, ApiError) as e:
        out["gaps"].append(f"tx fetch failed: {type(e).__name__}")
        return out
    if not isinstance(tx, dict):
        out["gaps"].append("tx not found: " + (errs[0] if errs else "no result"))
        return out
    out["found"], out["sender"], out["launcher_contract"] = True, (tx.get("from") or "").lower(), (tx.get("to") or "").lower()
    try:
        out["decoded"] = decode_launch_and_buy(tx.get("input", ""))
    except ValueError as e:
        out["gaps"].append(f"calldata malformed: {e}")
    if out["decoded"] is None and not out["gaps"]:
        out["gaps"].append(f"not a launchAndBuy call (selector {str(tx.get('input', ''))[:10]})")
    return out
