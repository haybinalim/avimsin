"""Bot tespit kuralları: kontrat cüzdan, gidiş-dönüş sweeper, yüksek frekans.

Pencereler zincir-duyarlıdır: süre eşiği `filters/chains.py` aralığına
bölünerek aralık sayısına çevrilir. Argsız çağrılar EVM varsayılanlarıyla
bit-bit aynı kalır — `filter.py` mevcut çağrıları değişmeden çalışır.
"""

from __future__ import annotations

from .base import WalletHistory, Verdict
from .chains import BLOCK_INTERVAL_SECONDS, DEFAULT_CHAIN, normalize_chain


class ContractRule:
    """Kontrat olan cüzdanları eler: router, havuz veya MEV botu olabilir; insan erken alıcısı değildir."""

    name = "contract"

    def check(self, history: WalletHistory) -> Verdict | None:
        if history.is_contract:
            return Verdict(
                self.name,
                "bot",
                f"{history.wallet} kontrat/program (zincir kontrat kontrolü pozitif)",
            )
        return None


class RoundTripRule:
    """Alım ile satım arası eşikten kısaysa sweeper kabul eder.

    Eşik iki yoldan verilir: blok cinsinden `max_gap` (EVM varsayılanı 10,
    argsız çağrı bit-bit aynı kalır) ya da süre cinsinden `max_gap_seconds`
    + `chain` (süre zincir aralığına bölünür, yukarı yuvarlanır, en az 1).
    İkisi birden verilirse `max_gap` kazanır.
    """

    name = "round_trip"

    def __init__(
        self,
        max_gap: int | None = 10,
        max_gap_seconds: float | None = None,
        chain: str = DEFAULT_CHAIN,
    ) -> None:
        self.chain = normalize_chain(chain)
        if max_gap is None:
            import math

            assert max_gap_seconds is not None
            max_gap = max(
                1,
                math.ceil(max_gap_seconds / BLOCK_INTERVAL_SECONDS[self.chain]),
            )
        self.max_gap = max_gap

    def check(self, history: WalletHistory) -> Verdict | None:
        if not history.received or not history.sent:
            return None
        first_in = min(t.block for t in history.received)
        first_out = min(t.block for t in history.sent)
        if 0 <= first_out - first_in < self.max_gap:
            return Verdict(
                self.name,
                "bot",
                f"alım (aralık {first_in}) sonrası {first_out - first_in} aralıkta satış — sweeper deseni",
            )
        return None


class HighFrequencyRule:
    """Pencere içinde `max_transfers`'ten fazla transfer yapan cüzdanı eler."""

    name = "high_frequency"

    def __init__(self, max_transfers: int = 20) -> None:
        self.max_transfers = max_transfers

    def check(self, history: WalletHistory) -> Verdict | None:
        count = len(history.transfers)
        if count > self.max_transfers:
            return Verdict(self.name, "bot", f"{count} transfer — insan işlem deseni değil")
        return None
