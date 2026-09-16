"""Fiyat-bağımsız, transfer miktarına dayalı kaçak satış göstergesi.

İlk pozitif, öz-transfer olmayan girişten başlayan kapalı penceredeki
pozitif çıkış miktarı / giriş miktarı `max_ratio`'dan büyükse cüzdan elenir.
Sıfır/negatif ve öz-transferler sayılmaz; mint/burn geçmiş modeliyle dışlanır.
Transferler gerçek alım/satım kanıtı değildir; fiyat ve maliyet verisi
olmadığından zarardayken satış koşulu değerlendirilmez.

Pencere zincir-duyarlıdır: `window_blocks` doğrudan verilebilir; verilmezse
`window_seconds` zincir aralığına bölünür. EVM varsayılanı 50_000 aralıktır.
"""

from __future__ import annotations

from .base import WalletHistory, Verdict
from .chains import BLOCK_INTERVAL_SECONDS, DEFAULT_CHAIN, normalize_chain


class DumpRule:
    """Kısa pencerede girişine oranla büyük token çıkışı olan cüzdanları eler."""

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
        received = history.received
        first_in = min(
            (t.block for t in received if t.value > 0 and t.frm != history.wallet),
            default=None,
        )
        if first_in is None:
            return None
        deadline = first_in + self.window_blocks
        received_in_window = sum(
            t.value for t in received
            if t.value > 0 and t.frm != history.wallet
            and first_in <= t.block <= deadline
        )
        sent_in_window = sum(
            t.value for t in history.sent
            if t.value > 0 and t.to != history.wallet
            and first_in <= t.block <= deadline
        )
        if received_in_window == 0 or sent_in_window == 0:
            return None
        ratio = sent_in_window / received_in_window
        if ratio > self.max_ratio:
            unit = "slot" if self.chain == "solana" else "blok"
            return Verdict(
                self.name,
                "dump",
                f"ilk girişten {self.window_blocks} {unit} içinde "
                f"giriş miktarı {received_in_window}, çıkış miktarı {sent_in_window} "
                f"ham token birimi ({ratio:.0%}); fiyat-bağımsız transfer göstergesi, "
                "gerçek satış veya zarar doğrulanmaz",
            )
        return None
