"""Skorlama motoru birim testleri: winrate, net P&L, frekans, bileşik skor ve sıralama."""

from __future__ import annotations

from avimsin.collectors.transfers import ZERO_ADDRESS, TransferEvent
from avimsin.filters.base import WalletHistory
from avimsin.scoring.engine import WalletScore, rank, score_wallet

W = "0x" + "a" * 40
POOL = "0x" + "b" * 40


def buy(block: int, value: int, tx: str | None = None) -> TransferEvent:
    return TransferEvent(frm=POOL, to=W, block=block, tx=tx or f"buy{block}", value=value)


def sell(block: int, value: int, tx: str | None = None) -> TransferEvent:
    return TransferEvent(frm=W, to=POOL, block=block, tx=tx or f"sell{block}", value=value)


def history(*events: TransferEvent) -> WalletHistory:
    return WalletHistory(wallet=W, transfers=list(events))


def test_no_sells_is_unscorable():
    assert score_wallet(history()) is None
    assert score_wallet(history(buy(1, 100), buy(2, 50))) is None


def test_burn_is_not_a_sell():
    burn = TransferEvent(frm=W, to=ZERO_ADDRESS, block=5, tx="burn", value=100)
    assert score_wallet(history(buy(1, 100), burn)) is None


def test_recovered_entry_and_profit_gives_full_win():
    s = score_wallet(history(buy(10, 100), sell(20, 60), sell(30, 60)))
    assert s is not None
    assert s.winrate == 1.0
    assert s.net_pnl == 20
    assert s.trades == 2
    assert s.frequency == round(2 / 20, 6)
    assert s.score == round(0.6 + 0.3 + 0.1 * (2 / 20), 3)


def test_partial_exit_is_loss():
    s = score_wallet(history(buy(10, 100), sell(20, 40)))
    assert s is not None
    assert s.winrate == 0.0
    assert s.net_pnl == -60
    assert s.score == round(0.1 * (1 / 10), 3)


def test_exact_break_even_counts_as_recovered_but_no_pnl_bonus():
    s = score_wallet(history(buy(1, 100), sell(2, 100)))
    assert s is not None
    assert s.winrate == 1.0
    assert s.net_pnl == 0
    # frekans 1/1 = 1.0 → 0.6 + 0 + 0.1
    assert s.score == 0.7


def test_sell_before_any_buy_does_not_count_as_recovery():
    # Mint ile gelen token alım sayılmaz; cum_in 0 iken satış "geri kazanım" değildir.
    mint = TransferEvent(frm=ZERO_ADDRESS, to=W, block=1, tx="mint", value=500)
    s = score_wallet(history(mint, sell(2, 300)))
    assert s is not None
    assert s.winrate == 0.0
    assert s.net_pnl == 300  # P&L yalnızca satış - alım (mint hariç)


def test_events_are_ordered_by_block_not_list_order():
    # Liste sırası karışık: önce satış görünse de blok sırasına göre alım önce gelir.
    s = score_wallet(history(sell(20, 150), buy(10, 100)))
    assert s is not None
    assert s.winrate == 1.0


def test_single_block_history_has_zero_frequency():
    s = score_wallet(history(buy(7, 100, "a"), sell(7, 200, "b")))
    assert s is not None
    assert s.frequency == 0.0
    assert s.score == 0.9


def test_frequency_contribution_is_capped():
    # 5 satış / 2 blok = 2.5 işlem/blok; skora katkısı 1.0 ile sınırlı.
    events = [buy(1, 1)] + [sell(b, 1, f"s{i}") for i, b in enumerate([2, 2, 2, 3, 3])]
    s = score_wallet(history(*events))
    assert s is not None
    assert s.frequency == 2.5
    assert s.score == 1.0


def test_rank_orders_by_score_then_pnl():
    a = WalletScore("a", 1.0, 10, 1, 0.0, 0.9)
    b = WalletScore("b", 1.0, 50, 1, 0.0, 0.9)
    c = WalletScore("c", 0.0, 999, 1, 0.0, 0.3)
    assert [s.wallet for s in rank([c, a, b])] == ["b", "a", "c"]
    assert rank([]) == []
