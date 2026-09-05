"""Kaçak satış (paper-hand) kuralı.

Fiyat-bağımsız sürüm: cüzdan, aldığı miktarın `max_ratio`'dan fazlasını
`window_blocks` içinde satarsa elenir. "Zarardayken" koşulu fiyat verisi
gerektirir; Faz 3'teki P&L skorlamasıyla birlikte zenginleştirilecek.
"""

from __future__ import annotations

from .base import WalletHistory, Verdict


class DumpRule:
    """Hızlı büyük satış yapan cüzdanları eler."""

    name = "dump"

    def __init__(self, max_ratio: float = 0.5, window_blocks: int = 50_000) -> None:
        self.max_ratio = max_ratio
        self.window_blocks = window_blocks

    def check(self, history: WalletHistory) -> Verdict | None:
        if not history.received:
            return None
        first_in = min(t.block for t in history.received)
        deadline = first_in + self.window_blocks
        sold_in_window = sum(t.block <= deadline for t in history.sent)
        if sold_in_window == 0:
            return None
        bought = len(history.received)
        ratio = sold_in_window / bought
        if ratio > self.max_ratio:
            return Verdict(
                self.name,
                "dump",
                f"alım sayısı {bought}, ilk alımdan {self.window_blocks} blok içinde satış {sold_in_window} ({ratio:.0%})",
            )
        return None
