"""CLI guards: an address reconstructed from a short display (0x2a71…82ec) shares its first 8 and last 4 hex
chars with the real one but traces clean — the typo check must shout before the hollow trace is trusted."""
from sleuth.cli import _typo_check

REAL = "0x2a716b891d0d550ccd3f417acff7a018eb8e82ec"


def test_near_match_against_watchlist_warns():
    garbled = REAL[:8] + "0" * 30 + REAL[-4:]
    warns = _typo_check([garbled], {REAL, "0x" + "1" * 40})
    assert len(warns) == 1 and REAL in warns[0] and "TYPO" in warns[0]


def test_exact_watchlist_member_and_unrelated_address_pass_silently():
    assert _typo_check([REAL], {REAL}) == []
    assert _typo_check(["0x" + "9" * 40], {REAL}) == []


def test_case_insensitive():
    garbled = (REAL[:8] + "0" * 30 + REAL[-4:]).upper().replace("0X", "0x")
    assert _typo_check([garbled], {REAL.upper().replace("0X", "0x")})
