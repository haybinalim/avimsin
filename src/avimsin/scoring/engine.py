"""Skorlama motoru: cüzdan transfer geçmişinden winrate, P&L ve frekans üretir.

Yaklaşım fiyat-bağımsızdır: P&L token birimi üzerinden hesaplanır (aldığından
fazlasını sattıysa pozitif). USD/P&L fiyat verisiyle Faz 4+ta zenginleşir.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..collectors.transfers import ZERO_ADDRESS, TransferEvent
from ..filters.base import WalletHistory


@dataclass(frozen=True)
class WalletScore:
    """Bir cüzdanın skorlama çıktısı."""

    wallet: str
    winrate: float          # 1.0: kümülatif satış girişi geri kazandı, 0.0: kazanamadı
    net_pnl: int            # toplam satılan - toplam alınan (ham token birimi)
    trades: int             # satış işlemi sayısı
    frequency: float        # blok başına işlem sıklığı
    score: float            # bileşik skor: 0..1 aralığına normalize


def score_wallet(history: WalletHistory) -> WalletScore | None:
    """Cüzdanı skorlar; satışı yoksa None (skorlanamaz) döndürür."""
    sells = history.sent
    if not sells:
        return None

    # Win tanımı (fiyat-bağımsız): kümülatif satışı, kümülatif alımı ilk geçen satış.
    # Cüzdan giriş maliyetini token bazında geri kazanmış olur; gerisi kâr.
    cum_in = 0
    cum_out = 0
    recovered = False
    events = sorted(
        history.transfers, key=lambda t: (t.block, t.tx)
    )
    for e in events:
        if e.to == history.wallet and e.frm != ZERO_ADDRESS:
            cum_in += e.value
        elif e.frm == history.wallet and e.to != ZERO_ADDRESS:
            cum_out += e.value
            if not recovered and cum_in > 0 and cum_out >= cum_in:
                recovered = True
    net_pnl = sum(t.value for t in sells) - sum(t.value for t in history.received)
    winrate = 1.0 if recovered else 0.0
    blocks = [t.block for t in history.transfers]
    span = max(blocks) - min(blocks) if blocks else 0
    frequency = len(sells) / span if span > 0 else 0.0

    # Bileşik: winrate ağırlıklı; P&L pozitifse bonus, frekans küçük katkı.
    score = min(1.0, 0.6 * winrate + 0.3 * (1.0 if net_pnl > 0 else 0.0) + 0.1 * min(frequency, 1.0))

    return WalletScore(
        wallet=history.wallet,
        winrate=round(winrate, 3),
        net_pnl=net_pnl,
        trades=len(sells),
        frequency=round(frequency, 6),
        score=round(score, 3),
    )


def rank(scores: list[WalletScore]) -> list[WalletScore]:
    """Skora göre azalan sıralar; eşitlikte P&L azalan."""
    return sorted(scores, key=lambda s: (-s.score, -s.net_pnl))
