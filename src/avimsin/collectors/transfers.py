"""Token transfer veri tipleri — ağ-bağımsız.

TransferEvent EVM (ERC-20) ve Solana (SPL) adaptörlerinin doldurduğu ortak
şekildir: frm/to adresleri, blok/slot, tx imzası, ham token değeri.
"""

from __future__ import annotations

from dataclasses import dataclass

# Mint/burn kaynak adresi: EVM'de 0x0. Solana adaptörü mint authority
# transferlerini de bu adresle işaretler ki filtreler ağ-farkı görmesin.
ZERO_ADDRESS = "0x" + "0" * 40


@dataclass(frozen=True)
class TransferEvent:
    """Tek bir token Transfer olayı."""

    frm: str
    to: str
    block: int  # EVM blok numarası / Solana slot numarası
    tx: str  # tx hash / imza
    value: int = 0  # ham token birimi (ondalık ölçeğiyle)
    token: str = ""  # hangi token: EVM kontrat adresi / SPL mint adresi
