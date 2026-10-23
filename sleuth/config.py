import json, os
from pathlib import Path

ROOT = Path(os.environ.get("SLEUTH_HOME", Path.home() / "sleuth"))
DATA = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"

DEFAULT = {
    "chains": {
        "4663": {
            "name": "Robinhood Chain",
            "explorer": "https://robinhoodchain.blockscout.com",
            "rpc": "https://rpc.mainnet.chain.robinhood.com",
            "tz": "America/Los_Angeles",
            # Liquidity venues whose token balance changes are swaps (auto-detection adds more per token).
            "known_pools": ["0x8366a39cc670b4001a1121b8f6a443a643e40951"],  # Uniswap-v4-style PoolManager
            # Swap-event topic0 hashes (verified from a live GH0ST receipt, 2026-10-22): independent evidence that a
            # contract is a trading venue / that a tx contained a swap. v2 Swap, v3 Swap, v4 PoolManager Swap.
            "swap_topics": [
                "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",
                "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
                "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",
            ],
            # Creator-fee escrows: internal ETH from these to a dev wallet is fee extraction, reported next to
            # the sell verdict ("never sold" alone hides a fee-harvesting launch farm — V4M, 2026-10-22).
            "fee_escrows": ["0xd3afeb2a57f70ef218aa82451c51b2fb0416ac9e"],  # PonsV2FeeEscrow
            # Launchpads: one eth_getLogs gives token, bonding-curve market and launcher wallet.
            "launchpads": [{
                "name": "Pons V2 (PonsV2LaunchAndBuy)",
                "address": "0xe33e9e479df8802cb0866d5d05258bec4cf62948",
                "topic": "0xdcacba5e347ae7abd91cb519eb877af8fa7774e347b85dd3ddcd24a2ba8cdf37",
                "token_topic": 1, "market_topic": 2, "launcher_topic": 3,
            }, {
                "name": "Pons V2 launcher (0x7ed598bc, found via GH0ST 2026-10-22)",
                "address": "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e",
                "topic": "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607",
                "token_topic": 1, "market_topic": 2, "launcher_topic": 3,
            }],
        }
    },
    "default_chain": "4663",
    "http": {"timeout": 20, "retries": 2, "concurrency": 6},
    "budgets": {"triage": 20, "trace": 70},
    # Services: evidence of a shared funding *service*, never of a shared owner.
    "known_services": {
        "0xf70da97812cb96acdf810712aa562db8dfa3dbef": "Relay bridge solver",
        "0xe68d0bbc023de3febda04f413db23ce9c5ea1934": "GasliteDrop batch airdrop",
        "0xd3afeb2a57f70ef218aa82451c51b2fb0416ac9e": "PonsV2FeeEscrow (creator fee claims)",
    },
    # Lowercased contract-name fragments that mark a creator-fee escrow on chains without a configured list.
    "fee_claim_markers": ["feeescrow", "feecollector", "feedistributor"],
    "service_name_markers": [
        "router", "pool", "depository", "hook", "factory", "locker", "settler",
        "burner", "batchtransfer", "escrow", "executor", "launch", "proxy",
        "bridge", "gateway", "inbox", "outbox", "paymaster", "entrypoint",
    ],
    "service_tx_threshold": 20000,
}


def load():
    cfg = json.loads(json.dumps(DEFAULT))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"config.json unreadable: {e}")
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    cfg["known_services"] = {k.lower(): v for k, v in cfg["known_services"].items()}
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "runs").mkdir(exist_ok=True)
    return cfg


def chain(cfg, chain_id=None):
    cid = str(chain_id or cfg["default_chain"])
    if cid not in cfg["chains"]:
        raise SystemExit(f"chain {cid} not configured; add it to {CONFIG_PATH}")
    return cid, cfg["chains"][cid]
