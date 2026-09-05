"""Filtre çerçevesi: kurallar cüzdan transfer geçmişine bakar, gerekçe döndürür.

Kurallar saf fonksiyonlardır: RPC'ye erişmez, sadece verilen geçmiyi değerlendirir.
İlk karar veren kural kazanır; karar None ise cüzdan bu kuraldan geçti demektir.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..collectors.transfers import TransferEvent, ZERO_ADDRESS

VERDICT_OK = "ok"
VERDICT_BOT = "bot"
VERDICT_DUMP = "dump"


@dataclass
class WalletHistory:
    """Bir cüzdanın tek token için bilinen transfer geçmişi."""

    wallet: str
    transfers: list[TransferEvent] = field(default_factory=list)
    is_contract: bool | None = None  # CLI doldurur; kurallar saf kalır

    @property
    def received(self) -> list[TransferEvent]:
        """Mint hariç gelen transferler."""
        return [t for t in self.transfers if t.to == self.wallet and t.frm != ZERO_ADDRESS]

    @property
    def sent(self) -> list[TransferEvent]:
        """Burn hariç giden transferler."""
        return [t for t in self.transfers if t.frm == self.wallet and t.to != ZERO_ADDRESS]


@dataclass(frozen=True)
class Verdict:
    """Bir kuralın kararı ve insan-okur gerekçesi."""

    rule: str
    verdict: str
    reason: str


class Rule(ABC):
    """Filtre kuralı sözleşmesi."""

    name: str

    @abstractmethod
    def check(self, history: WalletHistory) -> Verdict | None:
        """Cüzdanı değerlendirir; karar varsa Verdict, yoksa None döndürür."""
