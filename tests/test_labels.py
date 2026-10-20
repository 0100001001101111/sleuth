from sleuth.labels import classify
from fixtures import *  # noqa: F401,F403

CFG = {"known_services": {RELAY: "Relay bridge solver"}, "service_name_markers": ["router", "depository"], "service_tx_threshold": 20000}


def test_bridge_payout_eoa_is_service_not_owner():
    # 0xf78B2eDA...: an EOA with 8,729 txs fed only by the Arbitrum retryable bridge. Not a dev wallet.
    kind, label = classify({"hash": "0xf78b2eda000000000000000000000000000000ab", "is_contract": False}, {"tx": 25000}, CFG)
    assert kind == "service" and "high-volume" in label


def test_known_service_and_name_markers_and_7702():
    assert classify({"hash": RELAY, "is_contract": False}, {"tx": 5}, CFG)[0] == "service"
    assert classify({"hash": "0x1", "is_contract": True, "name": "RelayDepository"}, None, CFG)[0] == "service"
    assert classify({"hash": DEV, "is_contract": True, "proxy_type": "eip7702"}, None, CFG) == ("eoa", "EIP-7702 delegated wallet")
    assert classify({"hash": SIBS[0], "is_contract": False}, {"tx": 12}, CFG) == ("eoa", "wallet")
    assert classify({"hash": DEAD, "is_contract": False}, None, CFG)[0] == "dead"
