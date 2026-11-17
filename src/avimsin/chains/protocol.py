"""Zincir istemci sözleşmesi — collectors'ın gördüğü ağ-bağımsız yüzey.

Collectors ve scoring bu protokole bağımlıdır, EvmClient'a değil: yeni bir ağ
(Solana vb.) bu protokolü dolduran bir adaptör sınıfıyla eklenir.
Slot = blok (EVM) veya slot (Solana); değerler her zaman ham token birimidir.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    # Döngüsel import kırıcı: collectors, bu protokolü tip olarak kullanır;
    # çalışma zamanında yalnızca adaptörler TransferEvent'i somutlar.
    from ..collectors.transfers import TransferEvent, WalletTransfers


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


class ChainStreaming(Protocol):
    """İsteğe bağlı yetenekler (yalnız Solana adaptörü). Çağıranlar getattr ile arar;
    yoksa ChainClient.token_transfers yoluna dönülür (EVM davranışı değişmez)."""

    def first_slot(self, token: str) -> int | None:
        """Token'ın en eski işleminin slotu (oluşturma); yoksa None."""
        ...

    def iter_token_transfers(
        self, token: str, from_slot: int, to_slot: int
    ) -> Iterator[TransferEvent]:
        """token_transfers ile aynı olaylar, kronolojik ve tembel (erken durma için)."""
        ...

    def wallet_transfers(
        self, token: str, wallets: list[str], from_slot: int, to_slot: int
    ) -> WalletTransfers:
        """Yalnız verilen cüzdanların token transferleri (cüzdan bazlı geçmiş)."""
        ...

    def attach_tx_cache(self, path: str) -> None:
        """İşlem önbelleğini SQLite dosyasına bağlar."""
        ...
