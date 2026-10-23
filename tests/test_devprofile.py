"""devprofile is three-valued: a missing explorer page is UNKNOWN (None), never 'no fee claims' / 'no farm signals',
and positive evidence (escrow claims, batch funding minutes before launch) is extracted from one internal-ETH page."""
from datetime import datetime
from sleuth import devprofile

ADDR = "0x" + "a1" * 20
ESCROW = "0x" + "e5" * 20
GASLITE = "0x" + "6a" * 20
OTHER = "0x" + "0f" * 20
CHAIN = {"fee_escrows": [ESCROW]}
CFG = {"fee_claim_markers": ["feeescrow", "feecollector", "feedistributor"]}


def _item(frm, name=None, value=10 ** 18, to=ADDR, ts="2026-10-20T01:00:00.000000Z", blk=100, tx="0xclaim", **kw):
    return {"from": {"hash": frm, "name": name}, "to": {"hash": to}, "value": str(value),
            "timestamp": ts, "block_number": blk, "transaction_hash": tx, **kw}


class FakeClient:
    def tx_internal(self, h):
        return [{"to": {"hash": f"0x{i:040x}"}, "value": str(10 ** 15)} for i in range(31)] if h == "0xbatchtx" else []


def test_missing_page_is_unknown_never_no():
    assert devprofile.fee_claims(None, ADDR, CHAIN, CFG) is None
    assert devprofile.launch_funding(FakeClient(), None, ADDR, 200, 1792460000) is None


def test_fee_claims_match_escrow_address_and_name_marker_and_skip_reverted():
    page = {"items": [
        _item(ESCROW, value=2 * 10 ** 18, ts="2026-10-20T03:00:00.000000Z"),            # configured escrow
        _item(OTHER, name="PonsV9FeeEscrow", value=5 * 10 ** 17),                        # name-marker escrow
        _item(ESCROW, value=10 ** 18, error="execution reverted"),                       # reverted: moved nothing
        _item(OTHER, name=None, value=10 ** 18),                                         # plain transfer: not a claim
        _item(ESCROW, value=10 ** 18, to=OTHER),                                         # outgoing leg: not a credit
    ], "next_page_params": None}
    fc = devprofile.fee_claims(page, ADDR, CHAIN, CFG)
    assert fc["eth"] == 2.5 and fc["n"] == 2
    assert set(fc["sources"]) == {ESCROW, OTHER} and fc["latest"] == "2026-10-20T03:00:00.000000Z"
    assert fc["partial"] is False
    page["next_page_params"] = {"block_number": 1}
    assert devprofile.fee_claims(page, ADDR, CHAIN, CFG)["partial"] is True  # paged: never proof of 'no claims'


def test_launch_funding_flags_fresh_batch_funded_launcher():
    launch_time = datetime.fromisoformat("2026-10-20T01:04:00+00:00").timestamp()  # 4 min after funding
    page = {"items": [
        _item(GASLITE, name="GasliteDrop", value=61 * 10 ** 15, blk=150, tx="0xbatchtx"),
        _item(ESCROW, value=10 ** 18, blk=300, ts="2026-10-21T01:00:00.000000Z"),  # post-launch fee income: excluded
    ], "next_page_params": None}
    lf = devprofile.launch_funding(FakeClient(), page, ADDR, creation_block=200, launch_time=launch_time)
    assert len(lf["events"]) == 1 and lf["events"][0]["from"] == GASLITE
    assert lf["minutes_before_launch"] == 4.0
    assert lf["batch"] == {"tx": "0xbatchtx", "via": "GasliteDrop", "recipients": 31}
    assert any("min before launch" in s for s in lf["signals"])
    assert any("launch-farm pattern" in s and "31 recipients" in s for s in lf["signals"])


def test_launch_funding_without_creation_block_is_unknown():
    page = {"items": [_item(GASLITE, name="GasliteDrop", blk=150)], "next_page_params": None}
    # without the creation boundary, pre-launch funding cannot be separated from fee income: unknown, not empty
    assert devprofile.launch_funding(FakeClient(), page, ADDR, creation_block=None, launch_time=None) is None


def test_contradictory_post_launch_timestamp_produces_no_fresh_signal():
    launch_time = datetime.fromisoformat("2026-10-20T00:30:00+00:00").timestamp()  # BEFORE the funding ts
    page = {"items": [_item(GASLITE, name=None, blk=150)], "next_page_params": None}
    lf = devprofile.launch_funding(FakeClient(), page, ADDR, creation_block=200, launch_time=launch_time)
    assert lf["minutes_before_launch"] is None and lf["signals"] == []


def test_batch_event_found_even_when_not_first_and_recipients_deduped():
    class DupClient:
        def tx_internal(self, h):
            same = {"to": {"hash": "0x" + "b" * 40}, "value": str(10 ** 15)}
            return [same, same,  # duplicate recipient counts once
                    {"to": {"hash": "0x" + "c" * 40}, "value": str(10 ** 15), "error": "reverted"},  # reverted: no
                    {"to": {"hash": "0x" + "d" * 40}, "value": str(10 ** 15)}] if h == "0xbatchtx" else []
    page = {"items": [
        _item(OTHER, name=None, value=10 ** 15, blk=100, tx="0xplain"),                 # earlier plain funding
        _item(GASLITE, name="GasliteDrop", value=10 ** 16, blk=150, tx="0xbatchtx"),    # batch is the 2nd event
    ], "next_page_params": None}
    lf = devprofile.launch_funding(DupClient(), page, ADDR, creation_block=200, launch_time=None)
    assert lf["batch"] == {"tx": "0xbatchtx", "via": "GasliteDrop", "recipients": 2}


def test_dust_claim_keeps_positive_count():
    page = {"items": [_item(ESCROW, value=1)], "next_page_params": None}  # one wei
    fc = devprofile.fee_claims(page, ADDR, CHAIN, CFG)
    assert fc["eth"] == 0.0 and fc["n"] == 1  # consumers must key on n, never on the rounded eth
