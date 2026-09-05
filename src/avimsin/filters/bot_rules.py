"""Bot tespit kuralları: kontrat cüzdan, gidiş-dönüş sweeper, yüksek frekans."""

from __future__ import annotations

from .base import WalletHistory, Verdict


class ContractRule:
    """Kontrat olan cüzdanları eler: router, havuz veya MEV botu olabilir; insan erken alıcısı değildir."""

    name = "contract"

    def check(self, history: WalletHistory) -> Verdict | None:
        if history.is_contract:
            return Verdict(
                self.name,
                "bot",
                f"{history.wallet} kontrat (eth_getCode boş değil)",
            )
        return None


class RoundTripRule:
    """Alım ile satım arasındaki blok farkı `max_gap`'ten küçükse sweeper kabul eder."""

    name = "round_trip"

    def __init__(self, max_gap: int = 10) -> None:
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
                f"alım (blok {first_in}) sonrası {first_out - first_in} blokta satış — sweeper deseni",
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
