"""Zincir-duyarlı kural pencereleri — EVM paritesi + Solana sınır testleri."""

from __future__ import annotations

import pytest

from avimsin.collectors.transfers import TransferEvent
from avimsin.filters.base import WalletHistory
from avimsin.filters.bot_rules import RoundTripRule
from avimsin.filters.chains import BLOCK_INTERVAL_SECONDS, normalize_chain
from avimsin.filters.dump_rules import DumpRule

W = "0x" + "11" * 20
OTHER = "0x" + "22" * 20
COIN = "0x" + "aa" * 20


def ev(frm: str, to: str, block: int, tx: str = "0xtx") -> TransferEvent:
    return TransferEvent(frm=frm, to=to, block=block, tx=tx, value=100, token=COIN)


def hist(*events: TransferEvent) -> WalletHistory:
    return WalletHistory(wallet=W, transfers=list(events))


# --- EVM paritesi: argsız çağrı eski sabitlerle aynı ---


def test_roundtrip_evm_default_parity() -> None:
    assert RoundTripRule().max_gap == 10
    assert RoundTripRule(chain="robinhood").max_gap == 10
    # Eşik altı yakalar, eşik üstü bırakır (eski davranış)
    assert RoundTripRule().check(hist(ev(OTHER, W, 100), ev(W, OTHER, 109))) is not None
    assert RoundTripRule().check(hist(ev(OTHER, W, 100), ev(W, OTHER, 110))) is None


def test_dump_evm_default_parity() -> None:
    assert DumpRule().window_blocks == 50_000
    assert DumpRule(chain="robinhood").window_blocks == 50_000
    # 2 alım, 2 satış pencerede → oran 1.0 > 0.5 → dump (eski davranış)
    h = hist(ev(OTHER, W, 100, "t1"), ev(OTHER, W, 101, "t2"),
             ev(W, OTHER, 102, "t3"), ev(W, OTHER, 103, "t4"))
    assert DumpRule().check(h) is not None


def test_explicit_blocks_win_over_chain() -> None:
    assert RoundTripRule(max_gap=10, chain="solana").max_gap == 10
    assert DumpRule(window_blocks=50_000, chain="solana").window_blocks == 50_000


def test_unknown_chain_rejected() -> None:
    with pytest.raises(ValueError):
        RoundTripRule(chain="bogus")
    with pytest.raises(ValueError):
        DumpRule(chain="bogus")
    with pytest.raises(ValueError):
        normalize_chain("bogus")


# --- Solana sınır testleri ---


def test_solana_roundtrip_derived_window() -> None:
    rule = RoundTripRule(max_gap=None, max_gap_seconds=60.0, chain="solana")
    assert rule.max_gap == 150  # 60 / 0.4
    # 149 slot fark: yakalar; 150: bırakır
    assert rule.check(hist(ev(OTHER, W, 1000), ev(W, OTHER, 1149))) is not None
    assert rule.check(hist(ev(OTHER, W, 1000), ev(W, OTHER, 1150))) is None


def test_solana_roundtrip_evm_equivalent_diverges() -> None:
    # Aynı slot farkı, EVM penceresinde (10) yakalanmaz, Solana penceresinde (150) yakalanır:
    # zincir hızı farkı karar değiştirir — bu test farkın varlığını kilitler.
    h = hist(ev(OTHER, W, 1000), ev(W, OTHER, 1100))
    assert RoundTripRule().check(h) is None  # 100 >= 10
    sol = RoundTripRule(max_gap=None, max_gap_seconds=60.0, chain="solana")
    assert sol.check(h) is not None  # 100 < 150


def test_solana_dump_derived_window() -> None:
    rule = DumpRule(chain="solana")
    assert rule.window_blocks == 31_250  # 12_500 / 0.4
    h = hist(ev(OTHER, W, 1000, "t1"), ev(OTHER, W, 1001, "t2"),
             ev(W, OTHER, 1002, "t3"), ev(W, OTHER, 1003, "t4"))
    assert rule.check(h) is not None
    # Satış pencere dışında → temiz
    h2 = hist(ev(OTHER, W, 1000, "t1"), ev(OTHER, W, 1001, "t2"),
              ev(W, OTHER, 1000 + 31_251, "t3"), ev(W, OTHER, 1000 + 31_252, "t4"))
    assert rule.check(h2) is None
    # Mesaj slot dilinde
    assert "slot" in rule.check(h).reason


def test_solana_dump_ratio_boundary() -> None:
    rule = DumpRule(chain="solana")
    # 2 alım 1 satış → oran 0.5, eşik üstü değil → temiz (katı > karşılaştırması)
    h = hist(ev(OTHER, W, 1000, "t1"), ev(OTHER, W, 1001, "t2"), ev(W, OTHER, 1002, "t3"))
    assert rule.check(h) is None


def test_intervals_documented_per_chain() -> None:
    assert set(BLOCK_INTERVAL_SECONDS) == {"robinhood", "solana"}
    assert BLOCK_INTERVAL_SECONDS["solana"] == pytest.approx(0.4)
    assert BLOCK_INTERVAL_SECONDS["robinhood"] == pytest.approx(0.25)
