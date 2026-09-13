"""Zincir istemci sözleşmesi — collectors'ın gördüğü ağ-bağımsız yüzey.

Collectors ve scoring bu protokole bağımlıdır, EvmClient'a değil: yeni bir ağ
(Solana vb.) bu protokolü dolduran bir adaptör sınıfıyla eklenir.
Slot = blok (EVM) veya slot (Solana); değerler her zaman ham token birimidir.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    # Döngüsel import kırıcı: collectors, bu protokolü tip olarak kullanır;
    # çalışma zamanında yalnızca adaptörler TransferEvent'i somutlar.
    from ..collectors.transfers import TransferEvent


class ChainClient(Protocol):
    """Zincir adaptörlerinin doldurduğu minimal okuma sözleşmesi."""

    def block_number(self) -> int:
        """En güncel blok/slot numarası."""
        ...

    def token_transfers(
        self, token: str, from_slot: int, to_slot: int
    ) -> list[TransferEvent]:
        """Token'ın [from_slot, to_slot] aralığındaki Transfer olayları, kronolojik."""
        ...

    def is_contract(self, wallet: str) -> bool:
        """Adres program/kontrat mı? (EVM: eth_getCode; Solana: account owner kontrolü)"""
        ...

    def decimals(self, token: str) -> int:
        """Token'ın ondalık basamağı (P&L'in ham birimden token birimine çevirisi için)."""
        ...
