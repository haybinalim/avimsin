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


def ev(
    frm: str, to: str, block: int, tx: str = "0xtx", value: int = 100
) -> TransferEvent:
    return TransferEvent(frm=frm, to=to, block=block, tx=tx, value=value, token=COIN)


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
    # 2 alımda 200 birim giriş, 2 satışta 200 birim çıkış → oran 1.0 > 0.5 → dump
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


# --- Miktar sözleşmesi: olay sayısı değil, pozitif ham token birimi ---


def test_dump_many_buys_one_sell_is_not_dump() -> None:
    # 1000 alım olayı + 1 satış olayı: eski olay-sayısı kuralı %100 dump derdi;
    # miktarlar eşit → oran 1/1000 → temiz.
    events = [ev(OTHER, W, 100 + i, f"b{i}", value=1) for i in range(1000)]
    events.append(ev(W, OTHER, 200, "s0", value=1))
    assert DumpRule().check(hist(*events)) is None


def test_dump_fifty_one_percent_of_amount() -> None:
    # 100 birim giriş, 50 birim çıkış → %50, katı > ile temiz; 51 birim → dump.
    buys = [ev(OTHER, W, 100 + i, f"b{i}", value=1) for i in range(100)]
    assert DumpRule().check(hist(*buys, ev(W, OTHER, 300, "s0", value=50))) is None
    v = DumpRule().check(hist(*buys, ev(W, OTHER, 300, "s0", value=51)))
    assert v is not None
    assert "çıkış miktarı 51" in v.reason


def test_dump_partial_sales_equivalent_to_single_sale() -> None:
    # 100 birimi tek seferde satmakla parça parça (34+33+33) satmak eşdeğer.
    h = hist(
        ev(OTHER, W, 100, "b0"),
        ev(W, OTHER, 200, "s0", value=34),
        ev(W, OTHER, 201, "s1", value=33),
        ev(W, OTHER, 202, "s2", value=33),
    )
    assert DumpRule().check(h) is not None


def test_out_of_window_transfers_do_not_affect_result() -> None:
    # Pencere [ilk giriş, ilk giriş + window_blocks]: pencere öncesi çıkış ve
    # pencere sonrası alım sonuca karışmaz — sonradan gelen alım seyreltmez.
    late_buy_block = 100 + DumpRule().window_blocks + 1
    h = hist(
        ev(W, OTHER, 50, "pre", value=500),  # ilk girişten önce çıkış — sayılmaz
        ev(OTHER, W, 100, "b0", value=100),  # ilk giriş
        ev(W, OTHER, 150, "s0", value=51),   # pencere içinde %51 çıkış
        ev(OTHER, W, late_buy_block, "late", value=10_000),  # seyreltmez
    )
    v = DumpRule().check(h)
    assert v is not None
    assert "çıkış miktarı 51" in v.reason


def test_self_and_zero_value_transfers_not_economic() -> None:
    # Öz-transfer ve sıfır değerli olaylar ekonomik giriş/çıkış değildir;
    # sıfır değerli giriş ilk giriş kabul edilmez.
    h = hist(
        ev(OTHER, W, 100, "z", value=0),
        ev(W, W, 101, "self", value=999),
        ev(OTHER, W, 110, "b0", value=100),
        ev(W, OTHER, 120, "s0", value=60),
    )
    assert DumpRule().check(h) is not None
    assert DumpRule().check(
        hist(ev(OTHER, W, 100, "z0", value=0), ev(W, OTHER, 110, "z1", value=0))
    ) is None


def test_intervals_documented_per_chain() -> None:
    assert set(BLOCK_INTERVAL_SECONDS) == {"robinhood", "solana"}
    assert BLOCK_INTERVAL_SECONDS["solana"] == pytest.approx(0.4)
    assert BLOCK_INTERVAL_SECONDS["robinhood"] == pytest.approx(0.25)
