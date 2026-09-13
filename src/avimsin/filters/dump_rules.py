"""Kaçak satış (paper-hand) kuralı.

Fiyat-bağımsız sürüm: cüzdan, aldığı miktarın `max_ratio`'dan fazlasını
pencere içinde satarsa elenir. Pencere zincir-duyarlıdır: `window_blocks`
doğrudan verilebilir (EVM çağrıları için); verilmezse `window_seconds`
zincir aralığına bölünür. Argsız `DumpRule()` EVM varsayılanıyla
(50_000 aralık) bit-bit aynı kalır.
"Zarardayken" koşulu fiyat verisi gerektirir; Faz 3'teki P&L skorlamasıyla
birlikte zenginleştirilecek.
"""

from __future__ import annotations

from .base import WalletHistory, Verdict
from .chains import BLOCK_INTERVAL_SECONDS, DEFAULT_CHAIN, normalize_chain


class DumpRule:
    """Hızlı büyük satış yapan cüzdanları eler."""

    name = "dump"

    def __init__(
        self,
        max_ratio: float = 0.5,
        window_blocks: int | None = None,
        window_seconds: float = 12_500.0,  # 50_000 aralık × 0.25 sn (EVM eşdeğeri)
        chain: str = DEFAULT_CHAIN,
    ) -> None:
        self.max_ratio = max_ratio
        self.chain = normalize_chain(chain)
        if window_blocks is None:
            import math

            window_blocks = max(
                1, math.ceil(window_seconds / BLOCK_INTERVAL_SECONDS[self.chain])
            )
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
            unit = "slot" if self.chain == "solana" else "blok"
            return Verdict(
                self.name,
                "dump",
                f"alım sayısı {bought}, ilk alımdan {self.window_blocks} {unit} içinde satış {sold_in_window} ({ratio:.0%})",
            )
        return None
