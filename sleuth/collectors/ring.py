"""Coordinated-launch heuristic (versioned). From the Pons ring case: a bundled transaction buys for 15–25 exempt
wallets within 1–3 blocks of launch, capturing 82–86% of supply. This rule reports numbers and a three-valued verdict;
it is evidence of COORDINATION at launch, not of a rug and not of ownership."""
from ..labels import DEAD

RULE_VERSION = "coordinated-launch/0.1"
ZERO = "0x0000000000000000000000000000000000000000"


def coordinated_launch(records, launch_block, total_supply_raw, exemptions=(), market=None, blocks=3,
                       min_buyers=10, min_share=0.5, coverage_complete=True):
    """records: normalized Transfer records of the token ({from,to,value,block}). Early buyers = recipients within
    [launch_block, launch_block+blocks] excluding mint/zero/dead/market. Verdict True when >=min_buyers hold >=min_share
    of supply at the cutoff; False only with complete coverage; None otherwise."""
    if launch_block is None or not total_supply_raw:
        return {"rule_version": RULE_VERSION, "verdict": None, "reason": "launch block or total supply unknown"}
    ex = {e.lower() for e in exemptions}
    skip = set(DEAD) | {ZERO, (market or "").lower()}
    hi = launch_block + blocks
    held = {}
    for r in records:
        if r["block"] > hi:
            continue
        if r["to"] not in skip:
            held[r["to"]] = held.get(r["to"], 0) + r["value"]
        if r["from"] not in skip:
            held[r["from"]] = held.get(r["from"], 0) - r["value"]
    buyers = {a: v for a, v in held.items() if v > 0}
    share_raw = sum(buyers.values())
    share = share_raw / total_supply_raw
    ex_hit = {a for a in buyers if a in ex}
    met = len(buyers) >= min_buyers and share >= min_share
    verdict = True if met else (False if coverage_complete else None)
    return {"rule_version": RULE_VERSION, "verdict": verdict, "early_buyers": len(buyers), "early_share": round(share, 4),
            "early_share_raw": share_raw, "cutoff_block": hi, "exempt_count": len(ex), "exempt_buyers": len(ex_hit),
            "exempt_share": round(sum(buyers[a] for a in ex_hit) / total_supply_raw, 4) if ex_hit else 0.0,
            "thresholds": {"blocks": blocks, "min_buyers": min_buyers, "min_share": min_share},
            "reason": None if verdict is not None else "transfer coverage incomplete through the cutoff block"}
