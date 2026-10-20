"""Synthetic fixtures modelled on the STOXPREPS case (chain 4663). Addresses and tx hashes are the real ones."""
TOKEN = "0xb4d3a6097b765d094c0950ac03e917a2773eef4d"
DEV = "0x1d61c8801e6349e41a910f632bd2d9142673ff3c"
POOL = "0x8366a39cc670b4001a1121b8f6a443a643e40951"      # PoolManager
MARKET = "0x6f4ac34314b317ee4572e0d54cbecd3210c008d9"    # Pons bonding-curve market
ROUTER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"     # routing hop (nets to zero)
SETTLER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa2"
DEAD = "0x000000000000000000000000000000000000dead"
ZERO = "0x0000000000000000000000000000000000000000"
BURNER = "0x6bf43ca706faa8ea46803299c191484e82280652"
RELAY = "0xf70da97812cb96acdf810712aa562db8dfa3dbef"     # Relay bridge solver (service)
BATCH = "0x3f43479c0000000000000000000000000000c8c8"     # BatchTransfer contract (service)
FUNDER = "0x52c2b30cfc9a03159a331e997b91719b53b2fcac"
SIBS = ["0xdb62b463fbd400f37b944f36a2032e7370539b80", "0x4e91fc43e0a9bfbaf98d063eef393fe74211e624", "0xf092c5c2bb5a48430033e19dd387d01b23694eb4"]
BATCH_TX = "0xb1026840f3d1914948d7590ea1646409c4175518f0ddcaec47d6ec548659c885"
BURN_TX = "0x30207b5b7435efbcf2e3c8b27f7d8d044dadecf508661a1f24108cb337de6cdb"
BUY_BLOCK = 93072040
E = 10 ** 18


def rec(tx, block, frm, to, value, idx=0):
    return {"tx": tx, "block": block, "idx": idx, "from": frm, "to": to, "value": value}
