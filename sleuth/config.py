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
            # Launchpads: one eth_getLogs gives token, bonding-curve market and launcher wallet.
            "launchpads": [{
                "name": "Pons V2 (PonsV2LaunchAndBuy)",
                "address": "0xe33e9e479df8802cb0866d5d05258bec4cf62948",
                "topic": "0xdcacba5e347ae7abd91cb519eb877af8fa7774e347b85dd3ddcd24a2ba8cdf37",
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
    },
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
